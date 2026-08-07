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
        self.memory = MODULE.ProjectMemory(root / "data", root / "config")
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

    def test_general_memory_rejects_credentials(self):
        with self.assertRaises(MODULE.MemoryError):
            sensitive_action = "use " + "password" + "=" + "fixture-value"
            self.memory.note_repetition({**self.root_arg, "problem": "login", "action": sensitive_action})

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
        self.assertEqual(registry["schema_version"], 2)
        self.assertEqual(registry["projects"][str(root)]["project_key"], "Legacy")

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
