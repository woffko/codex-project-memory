#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("project_memory_mcp.py")
SPEC = importlib.util.spec_from_file_location("project_memory_mcp", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProjectMemoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.project.mkdir()
        self.memory = MODULE.ProjectMemory(
            root / "data",
            root / "config",
            longrun_state_dir=root / "longrun-state",
        )
        self.entry = self.memory.enroll(str(self.project), "pilot", True, project_key="pilot")
        self.root_arg = {"project_root": str(self.project)}

    def tearDown(self):
        self.temporary.cleanup()

    def test_repeated_candidate_requires_two_occurrences_and_verification(self):
        args = {**self.root_arg, "problem": "test appliance queue stalls", "action": "restart test queue", "observation": "first attempt", "tags": ["test-appliance"]}
        first = self.memory.note_repetition(args)
        with self.assertRaises(MODULE.MemoryError):
            self.memory.finalize_solution({**self.root_arg, "candidate_id": first["id"], "title": "Stable queue recovery", "final_steps": ["restart queue"], "verification": "traffic passed", "outcome": "stable"})
        second = self.memory.note_repetition({**args, "observation": "second attempt"})
        solution = self.memory.finalize_solution({**self.root_arg, "candidate_id": second["id"], "title": "Stable queue recovery", "final_steps": ["restart queue", "run quality test"], "verification": "quality test passed twice", "outcome": "queue stayed stable", "tags": ["verified"]})
        self.assertEqual(solution["kind"], "solution")
        self.assertEqual(solution["repetition_count"], 2)
        results = self.memory.search({**self.root_arg, "query": "quality stable"})["results"]
        self.assertEqual(results[0]["id"], solution["id"])

    def test_test_asset_secret_is_encrypted_and_only_revealed_explicitly(self):
        credential_value = "fixture-" + "credential-987"
        asset = self.memory.store_test_asset({**self.root_arg, "test_only": True, "name": "Lab appliance", "asset_type": "test appliance", "endpoint": "192.0.2.10", "username": "operator", "secret_fields": {"credential": credential_value}, "paths": {"system_log": "/tmp/system.log"}, "notes": "test bench"})
        ordinary = self.memory.get({**self.root_arg, "record_id": asset["id"]})
        self.assertNotIn("secret_fields", ordinary)
        revealed = self.memory.get({**self.root_arg, "record_id": asset["id"]}, include_test_secrets=True)
        self.assertEqual(revealed["secret_fields"]["credential"], credential_value)
        db_path = self.memory._db_path(self.entry)
        self.assertNotIn(credential_value.encode(), db_path.read_bytes())

    def test_test_asset_secret_stages_as_handle_without_mcp_disclosure(self):
        credential_value = "fixture-" + "longrun-password-987"
        asset = self.memory.store_test_asset({
            **self.root_arg,
            "test_only": True,
            "name": "Lab appliance",
            "asset_type": "test appliance",
            "endpoint": "192.0.2.20",
            "username": "root",
            "secret_fields": {"password": credential_value},
        })
        staged = MODULE.call_tool(
            self.memory,
            "project_memory_stage_test_asset_for_longrun",
            {
                **self.root_arg,
                "record_id": asset["id"],
                "secret_field": "password",
            },
        )
        secret_id = staged["stdin_secret_id"]
        self.assertRegex(secret_id, r"^[a-f0-9]{32}$")
        self.assertFalse(staged["secret_value_returned"])
        self.assertTrue(staged["output_suppression_required"])
        path = self.memory.longrun_state_dir / "secrets" / f"{secret_id}.stdin"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_bytes(), credential_value.encode() + b"\n")

        database_bytes = self.memory._db_path(self.entry).read_bytes()
        metrics_files = list(self.memory.metrics.path.parent.glob("usage.sqlite3*"))
        metrics_bytes = b"".join(value.read_bytes() for value in metrics_files)
        rendered = MODULE.compact_json(staged).encode()
        self.assertNotIn(credential_value.encode(), database_bytes)
        self.assertNotIn(credential_value.encode(), metrics_bytes)
        self.assertNotIn(credential_value.encode(), rendered)
        self.assertNotIn(secret_id.encode(), database_bytes)
        self.assertNotIn(secret_id.encode(), metrics_bytes)
        audit = self.memory._connect(self.entry)
        detail = audit.execute(
            "SELECT detail_json FROM audit WHERE action='stage_test_asset_for_longrun' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        audit.close()
        self.assertNotIn(credential_value, detail)
        self.assertNotIn(secret_id, detail)

        with self.assertRaises(MODULE.ReportedToolError):
            MODULE.call_tool(
                self.memory,
                "project_memory_stage_test_asset_for_longrun",
                {
                    **self.root_arg,
                    "record_id": asset["id"],
                    "secret_field": "missing",
                },
            )

    def test_general_memory_rejects_credentials(self):
        with self.assertRaises(MODULE.MemoryError):
            sensitive_action = "use " + "password" + "=" + "fixture-value"
            self.memory.note_repetition({**self.root_arg, "problem": "login", "action": sensitive_action})

    def test_longrun_staging_rolls_back_file_when_audit_fails(self):
        asset = self.memory.store_test_asset({
            **self.root_arg,
            "test_only": True,
            "name": "Audit fixture",
            "asset_type": "test appliance",
            "secret_fields": {"password": "audit-fixture-value"},
        })
        with patch.object(self.memory, "_audit", side_effect=RuntimeError("fixture audit failure")):
            with self.assertRaises(RuntimeError):
                self.memory.stage_test_asset_for_longrun({
                    **self.root_arg,
                    "record_id": asset["id"],
                    "secret_field": "password",
                })
        secrets_dir = self.memory.longrun_state_dir / "secrets"
        self.assertEqual(list(secrets_dir.glob("*.stdin")), [])

    def test_longrun_staging_rejects_symlink_secret_directory(self):
        asset = self.memory.store_test_asset({
            **self.root_arg,
            "test_only": True,
            "name": "Symlink fixture",
            "asset_type": "test appliance",
            "secret_fields": {"password": "symlink-fixture-value"},
        })
        self.memory.longrun_state_dir.mkdir(parents=True, mode=0o700)
        target = Path(self.temporary.name) / "unsafe-secrets"
        target.mkdir()
        (self.memory.longrun_state_dir / "secrets").symlink_to(target)
        with self.assertRaises(MODULE.MemoryError):
            self.memory.stage_test_asset_for_longrun({
                **self.root_arg,
                "record_id": asset["id"],
                "secret_field": "password",
            })
        self.assertEqual(list(target.iterdir()), [])

    def test_unenrolled_checkout_is_rejected(self):
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        with self.assertRaises(MODULE.MemoryError):
            self.memory.status({"project_root": str(other)})

    def test_name_and_project_key_defaults_and_overrides_are_independent(self):
        derived = Path(self.temporary.name) / "Derived-Project"
        derived.mkdir()
        entry = self.memory.enroll(str(derived))
        self.assertEqual(entry["project_name"], "Derived-Project")
        self.assertEqual(entry["project_key"], "Derived-Project")
        self.assertIsNone(entry["parent_project"])
        self.assertFalse(entry["is_meta_project"])

        renamed = Path(self.temporary.name) / "renamed-project"
        renamed.mkdir()
        friendly = self.memory.enroll(str(renamed), project_name="Friendly Display Name")
        self.assertEqual(friendly["project_name"], "Friendly Display Name")
        self.assertEqual(friendly["project_key"], "renamed-project")

    def test_subproject_uses_hierarchical_key_and_parent_becomes_meta(self):
        child = self.project / "OpenMeta-c"
        child.mkdir()
        entry = self.memory.enroll(str(child), parent_root=str(self.project))
        self.assertEqual(entry["project_key"], "pilot/OpenMeta-c")
        self.assertEqual(entry["parent_project"], "pilot")

        parent_status = self.memory.status({"project": "pilot"})
        child_status = self.memory.status({"project": "pilot/OpenMeta-c"})
        self.assertTrue(parent_status["is_meta_project"])
        self.assertEqual(parent_status["children"], ["pilot/OpenMeta-c"])
        self.assertEqual(child_status["parent_project"], "pilot")
        self.assertFalse(child_status["is_meta_project"])

    def test_project_key_selects_exact_database_and_root_remains_compatible(self):
        child = self.project / "rust"
        child.mkdir()
        self.memory.enroll(str(child), parent_root=str(self.project))
        self.memory.note_repetition({"project": "pilot", "problem": "shared fixture unavailable", "action": "restart fixture"})
        self.memory.note_repetition({"project": "pilot/rust", "problem": "rust compiler warning", "action": "adjust rust flags"})

        shared = self.memory.search({"project": "pilot", "query": "fixture"})
        rust = self.memory.search({"project": "pilot/rust", "query": "compiler"})
        self.assertEqual(shared["project"], "pilot")
        self.assertEqual(len(shared["results"]), 1)
        self.assertEqual(rust["project"], "pilot/rust")
        self.assertEqual(len(rust["results"]), 1)
        self.assertEqual(self.memory.status(self.root_arg)["project"], "pilot")
        with self.assertRaises(MODULE.MemoryError):
            self.memory.status({"project": "pilot/rust", "project_root": str(self.project)})

    def test_same_root_projects_have_independent_memory_and_default_resolution(self):
        gui = self.memory.enroll(
            str(self.project),
            "Pilot GUI",
            project_key="pilot/gui",
            parent_project="pilot",
        )
        reenrolled = self.memory.enroll(str(self.project), project_key="PILOT/GUI")

        self.assertNotEqual(gui["project_id"], self.entry["project_id"])
        self.assertEqual(reenrolled["project_id"], gui["project_id"])
        self.assertEqual(gui["parent_project"], "pilot")
        self.assertEqual(self.memory.status(self.root_arg)["project"], "pilot")
        gui_status = self.memory.status(
            {"project": "pilot/gui", "project_root": str(self.project)}
        )
        self.assertFalse(gui_status["is_default_for_root"])
        self.assertEqual(gui_status["same_root_projects"], ["pilot", "pilot/gui"])
        self.assertEqual(self.memory.status({"project": "pilot"})["children"], ["pilot/gui"])
        self.assertNotEqual(self.memory._db_path(self.entry), self.memory._db_path(gui))

        self.memory.note_repetition(
            {"project": "pilot", "problem": "core queue stalls", "action": "restart core queue"}
        )
        self.memory.note_repetition(
            {"project": "pilot/gui", "problem": "gui panel stalls", "action": "restart gui panel"}
        )
        self.assertEqual(len(self.memory.search({"project": "pilot", "query": "core"})["results"]), 1)
        self.assertEqual(len(self.memory.search({"project": "pilot", "query": "gui"})["results"]), 0)
        self.assertEqual(len(self.memory.search({"project": "pilot/gui", "query": "gui"})["results"]), 1)
        self.assertEqual(len(self.memory.search({"project": "pilot/gui", "query": "core"})["results"]), 0)
        default_backup = self.memory.backup(project_root=str(self.project))
        gui_backup = self.memory.backup(project="pilot/gui")
        self.assertEqual(default_backup.parent.parent, self.memory._db_path(self.entry).parent)
        self.assertEqual(gui_backup.parent.parent, self.memory._db_path(gui).parent)
        self.assertNotEqual(default_backup.parent.parent, gui_backup.parent.parent)

        other = Path(self.temporary.name) / "other-root"
        other.mkdir()
        with self.assertRaises(MODULE.MemoryError):
            self.memory.status({"project": "pilot/gui", "project_root": str(other)})

    def test_attaching_existing_project_preserves_id_and_encrypted_records(self):
        child = self.project / "legacy-child"
        child.mkdir()
        original = self.memory.enroll(str(child), "legacy-child", True)
        asset = self.memory.store_test_asset({
            "project": "legacy-child",
            "test_only": True,
            "name": "Shared test fixture",
            "asset_type": "test fixture",
            "secret_fields": {"credential": "fixture-value"},
        })

        attached = self.memory.enroll(str(child), parent_root=str(self.project))
        self.assertEqual(attached["project_id"], original["project_id"])
        self.assertEqual(attached["project_key"], "pilot/legacy-child")
        self.assertTrue(attached["allow_test_secrets"])
        revealed = self.memory.get(
            {"project": "pilot/legacy-child", "record_id": asset["id"]},
            include_test_secrets=True,
        )
        self.assertEqual(revealed["secret_fields"]["credential"], "fixture-value")

    def test_schema_v1_registry_gains_project_key_without_changing_id(self):
        root = Path(self.temporary.name) / "legacy-root"
        root.mkdir()
        data = Path(self.temporary.name) / "legacy-data"
        data.mkdir()
        registry_path = data / "registry.json"
        registry_path.write_text(json.dumps({
            "schema_version": 1,
            "projects": {
                str(root): {
                    "project_id": "legacy-id",
                    "project_name": "Legacy",
                    "project_root": str(root),
                    "git_remote": "",
                    "allow_test_secrets": False,
                    "enrolled_at": "2026-01-01T00:00:00+00:00",
                }
            },
        }), encoding="utf-8")
        memory = MODULE.ProjectMemory(data, Path(self.temporary.name) / "legacy-config")

        status = memory.status({"project": "Legacy"})
        self.assertEqual(status["project_id"], "legacy-id")
        persisted = memory.enroll(str(root))
        self.assertEqual(persisted["project_id"], "legacy-id")
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["schema_version"], 3)
        self.assertEqual(registry["projects"]["legacy-id"]["project_key"], "Legacy")
        self.assertEqual(registry["roots"][str(root)]["default_project_id"], "legacy-id")
        self.assertEqual(registry["roots"][str(root)]["project_ids"], ["legacy-id"])

    def test_schema_v2_migration_preserves_id_parent_and_encrypted_record(self):
        root = Path(self.temporary.name) / "v2-root"
        child = root / "child"
        child.mkdir(parents=True)
        data = Path(self.temporary.name) / "v2-data"
        config = Path(self.temporary.name) / "v2-config"
        original = MODULE.ProjectMemory(data, config)
        parent_entry = original.enroll(str(root), allow_test_secrets=True, project_key="v2")
        child_entry = original.enroll(str(child), parent_root=str(root))
        asset = original.store_test_asset({
            "project": "v2",
            "test_only": True,
            "name": "Migration fixture",
            "asset_type": "test fixture",
            "secret_fields": {"credential": "migration-fixture-value"},
        })
        MODULE.call_tool(original, "project_memory_status", {"project": "v2"})
        registry_path = data / "registry.json"
        v3_registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry_path.write_text(
            json.dumps({
                "schema_version": 2,
                "projects": {
                    entry["project_root"]: entry
                    for entry in v3_registry["projects"].values()
                },
            }),
            encoding="utf-8",
        )
        original.metrics.close()

        migrated = MODULE.ProjectMemory(data, config)
        self.assertEqual(migrated.status({"project_root": str(root)})["project_id"], parent_entry["project_id"])
        self.assertEqual(migrated.status({"project": "v2/child"})["parent_project"], "v2")
        revealed = migrated.get(
            {"project": "v2", "record_id": asset["id"]},
            include_test_secrets=True,
        )
        self.assertEqual(revealed["secret_fields"]["credential"], "migration-fixture-value")
        persisted = migrated.enroll(str(root))
        self.assertEqual(persisted["project_id"], parent_entry["project_id"])
        self.assertEqual(migrated.resolve_project_key("v2/child")["project_id"], child_entry["project_id"])
        self.assertEqual(
            migrated.metrics_report(project="v2", since_days=30)["projects"][0]["probes"],
            1,
        )
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["schema_version"], 3)
        self.assertIn(parent_entry["project_id"], registry["projects"])
        self.assertIn(child_entry["project_id"], registry["projects"])
        migrated.metrics.close()

    def test_duplicate_key_and_out_of_tree_subproject_are_rejected(self):
        duplicate = Path(self.temporary.name) / "duplicate"
        duplicate.mkdir()
        with self.assertRaises(MODULE.MemoryError):
            self.memory.enroll(str(duplicate), project_key="pilot")
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        with self.assertRaises(MODULE.MemoryError):
            self.memory.enroll(str(outside), parent_root=str(self.project))

    def test_every_mcp_tool_accepts_project_or_legacy_project_root(self):
        for tool in MODULE.TOOLS:
            schema = tool["inputSchema"]
            self.assertIn("project", schema["properties"])
            self.assertIn("project_root", schema["properties"])
            self.assertEqual(schema["anyOf"], [{"required": ["project"]}, {"required": ["project_root"]}])

    def test_default_storage_respects_xdg_locations(self):
        root = Path(self.temporary.name)
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "xdg-data"), "XDG_CONFIG_HOME": str(root / "xdg-config")}, clear=False):
            memory = MODULE.ProjectMemory()
        self.assertEqual(memory.data_home, root / "xdg-data/codex-project-memory")
        self.assertEqual(memory.config_home, root / "xdg-config/codex-project-memory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
