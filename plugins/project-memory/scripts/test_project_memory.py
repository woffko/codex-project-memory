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
        self.entry = self.memory.enroll(str(self.project), "pilot", True)
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

    def test_default_storage_respects_xdg_locations(self):
        root = Path(self.temporary.name)
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "xdg-data"), "XDG_CONFIG_HOME": str(root / "xdg-config")}, clear=False):
            memory = MODULE.ProjectMemory()
        self.assertEqual(memory.data_home, root / "xdg-data/codex-project-memory")
        self.assertEqual(memory.config_home, root / "xdg-config/codex-project-memory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
