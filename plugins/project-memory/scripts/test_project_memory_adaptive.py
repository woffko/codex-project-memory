#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import project_memory_mcp as module


class AdaptiveProjectMemoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.project.mkdir()
        self.memory = module.ProjectMemory(root / "data", root / "config")
        self.entry = self.memory.enroll(str(self.project), "Pilot", project_key="Pilot")

    def tearDown(self):
        self.memory.metrics.close()
        self.temporary.cleanup()

    def remember_solution(self, stable_key: str, problem: str, *, project: str = "Pilot", title: str = "Verified recovery"):
        values = {
            "project": project,
            "operation": "occurrence",
            "stable_key": stable_key,
            "title": title,
            "problem": problem,
            "context": "lab station 40",
            "action": ["cancel motion", "pulse acknowledge", "wait for Ready"],
            "applicability": {"scope": "station 40", "versions": ["v2"], "devices": ["fixture drive"]},
            "constraints": ["Turn remains required"],
            "warnings": ["Do not bypass the safety sequence"],
            "verification": ["Ready=true", "Error=false"],
            "outcome": "drive ready",
            "observation": "bounded recovery worked",
        }
        first = self.memory.remember(values)
        second = self.memory.remember({**values, "verified": True})
        self.assertFalse(first["auto_finalized"])
        self.assertTrue(second["auto_finalized"])
        return second

    def test_verified_complete_exact_match_returns_one_call_action(self):
        solution = self.remember_solution("festo-e521-station40", "Drive reports E521")
        result = self.memory.recall({"project": "Pilot", "query": "festo-e521-station40"})
        self.assertEqual(result["mode_used"], "compact")
        self.assertEqual(result["direct_action"]["record"]["id"], solution["id"])
        self.assertEqual(result["direct_action"]["record"]["verification"], ["Ready=true", "Error=false"])
        self.assertTrue(result["coverage"]["constraints"])
        self.assertLessEqual(result["budget"]["actual_bytes"], result["budget"]["max_bytes"])

    def test_important_failure_is_in_evidence_not_action(self):
        solution = self.remember_solution("queue-recovery", "Fixture queue stalls")
        self.memory.remember({
            "project": "Pilot",
            "operation": "failed_attempt",
            "record_id": solution["id"],
            "importance": "important",
            "attempt": "disable Turn permanently",
            "observed_result": "normal operation could not resume",
            "why_it_failed": "Turn is an operating condition",
            "do_not_repeat": "keep Turn enabled",
        })
        action = self.memory.read({"project": "Pilot", "record_id": solution["id"], "view": "action"})
        evidence = self.memory.read({"project": "Pilot", "record_id": solution["id"], "view": "evidence"})
        self.assertNotIn("important_failures", action)
        self.assertEqual(len(evidence["important_failures"]), 1)
        self.assertIn("events", action["omitted"])

    def test_parent_and_child_are_recalled_without_sibling_search(self):
        child = self.project / "child"
        child.mkdir()
        self.memory.enroll(str(child), parent_root=str(self.project))
        self.memory.remember({
            "project": "Pilot",
            "operation": "upsert",
            "kind": "constraint",
            "stable_key": "shared-safety",
            "title": "Shared safety boundary",
            "statement": "Never bypass the interlock",
            "reason": "The fixture can move",
            "scope": "all children",
            "severity": "critical",
            "conditions": [],
            "confidence": "high",
        })
        result = self.memory.recall({"project": "Pilot/child", "query": "interlock"})
        self.assertEqual(result["searched_projects"], ["Pilot/child", "Pilot"])
        self.assertEqual(result["direct_action"]["record"]["kind"], "constraint")

    def test_conflict_blocks_direct_action(self):
        first = self.remember_solution("recovery-a", "conflicting recovery alpha", title="Recovery alpha")
        second = self.remember_solution("recovery-b", "conflicting recovery beta", title="Recovery beta")
        self.memory.remember({
            "project": "Pilot",
            "operation": "upsert",
            "kind": "solution",
            "stable_key": "recovery-b",
            "record_id": second["id"],
            "relations": [{"relation": "contradicts", "target_id": first["id"]}],
        })
        result = self.memory.recall({"project": "Pilot", "query": "recovery-b"})
        self.assertIsNone(result["direct_action"])
        self.assertEqual(result["reason"], "conflict")
        self.assertEqual(len(result["conflicts"]), 1)

    def test_superseded_record_becomes_cold_and_is_not_current(self):
        old = self.remember_solution("old-recovery", "old reset procedure")
        new = self.remember_solution("new-recovery", "new reset procedure")
        self.memory.remember({
            "project": "Pilot",
            "operation": "upsert",
            "kind": "solution",
            "stable_key": "new-recovery",
            "record_id": new["id"],
            "supersedes_id": old["id"],
        })
        old_read = self.memory.read({"project": "Pilot", "record_id": old["id"], "view": "card"})
        self.assertEqual(old_read["importance"], "cold")
        self.assertEqual(old_read["staleness_state"], "superseded")
        result = self.memory.recall({"project": "Pilot", "query": "old reset procedure"})
        self.assertEqual(result["reason"], "no_results")

    def test_expired_checkpoint_is_excluded_from_continuation(self):
        expires = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(microsecond=0).isoformat()
        checkpoint = self.memory.remember({
            "project": "Pilot",
            "operation": "checkpoint",
            "stable_key": "adaptive-retrieval-work",
            "title": "Adaptive retrieval work",
            "goal": "finish adaptive retrieval",
            "completed": ["views"],
            "current_state": "ranking untested",
            "next_steps": ["run benchmark"],
            "blockers": [],
            "branch": "deepdive",
            "head_commit": "abc123",
            "expires_at": expires,
            "confidence": "high",
        })
        self.assertEqual(checkpoint["kind"], "checkpoint")
        result = self.memory.recall({"project": "Pilot", "query": "continue adaptive retrieval", "task_type": "continuation"})
        self.assertEqual(result["reason"], "no_results")

    def test_bound_lean_server_omits_selectors_and_rejects_cross_scope(self):
        bound = module.ProjectMemory(self.memory.data_home, self.memory.config_home, bound_project="Pilot", profile="lean")
        tools = module.exposed_tools(bound)
        self.assertEqual({tool["name"] for tool in tools}, module.LEAN_TOOL_NAMES)
        for tool in tools:
            self.assertNotIn("project", tool["inputSchema"]["properties"])
            self.assertNotIn("project_root", tool["inputSchema"]["properties"])
        self.assertEqual(bound.status({})["project"], "Pilot")
        with self.assertRaises(module.MemoryError):
            bound.status({"project": "Other"})
        bound.metrics.close()

    def test_content_migration_moves_hot_history_once_and_creates_backup(self):
        db_path = self.memory._db_path(self.entry)
        self.memory.metrics.close()
        db_path.unlink()
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE records (
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',
                problem TEXT NOT NULL DEFAULT '',context TEXT NOT NULL DEFAULT '',payload_json TEXT NOT NULL,
                secret_blob BLOB,fingerprint TEXT,repetition_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'active',
                revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE revisions(record_id TEXT NOT NULL,revision INTEGER NOT NULL,snapshot_json TEXT NOT NULL,changed_at TEXT NOT NULL,PRIMARY KEY(record_id,revision));
            CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,record_id TEXT,detail_json TEXT NOT NULL,created_at TEXT NOT NULL);
            """
        )
        payload = {"action": "restart fixture", "observations": [{"at": "2026-01-01T00:00:00+00:00", "text": "worked once"}], "tags": ["fixture"]}
        connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy", "candidate", "Legacy candidate", "", "queue stalls", "lab", json.dumps(payload), None, "fp", 1, "active", 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()
        migrated = self.memory.migrate_databases(project="Pilot")
        self.assertTrue(migrated["projects"][0]["changed"])
        self.assertTrue(Path(migrated["projects"][0]["backup"]).exists())
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        self.assertEqual(db.execute("SELECT value FROM memory_metadata WHERE key='schema_version'").fetchone()[0], "2")
        self.assertNotIn("observations", json.loads(db.execute("SELECT payload_json FROM records WHERE id='legacy'").fetchone()[0]))
        self.assertEqual(db.execute("SELECT count(*) FROM record_events WHERE record_id='legacy' AND event_type='occurrence'").fetchone()[0], 1)
        first_count = db.execute("SELECT count(*) FROM record_events WHERE record_id='legacy'").fetchone()[0]
        db.close()
        second = self.memory.migrate_databases(project="Pilot")
        self.assertFalse(second["projects"][0]["changed"])
        db = sqlite3.connect(db_path)
        self.assertEqual(db.execute("SELECT count(*) FROM record_events WHERE record_id='legacy'").fetchone()[0], first_count)
        db.close()

    def test_failed_content_migration_rolls_back_record_rewrite_and_version(self):
        db_path = self.memory._db_path(self.entry)
        db_path.unlink()
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE records (
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',
                problem TEXT NOT NULL DEFAULT '',context TEXT NOT NULL DEFAULT '',payload_json TEXT NOT NULL,
                secret_blob BLOB,fingerprint TEXT,repetition_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'active',
                revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE revisions(record_id TEXT NOT NULL,revision INTEGER NOT NULL,snapshot_json TEXT NOT NULL,changed_at TEXT NOT NULL,PRIMARY KEY(record_id,revision));
            CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,record_id TEXT,detail_json TEXT NOT NULL,created_at TEXT NOT NULL);
            """
        )
        payload = {"action": "restart fixture", "observations": [{"text": "worked"}]}
        connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("rollback", "candidate", "Rollback", "", "stalls", "lab", json.dumps(payload), None, "fp", 1, "active", 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()
        with patch.object(self.memory, "_refresh_record", side_effect=RuntimeError("fixture migration failure")):
            with self.assertRaises(RuntimeError):
                self.memory._connect(self.entry)
        connection = sqlite3.connect(db_path)
        self.assertIn("observations", json.loads(connection.execute("SELECT payload_json FROM records WHERE id='rollback'").fetchone()[0]))
        version = connection.execute("SELECT value FROM memory_metadata WHERE key='schema_version'").fetchone()
        self.assertIsNone(version)
        self.assertEqual(connection.execute("SELECT count(*) FROM record_events").fetchone()[0], 0)
        connection.close()

    def test_new_structured_fields_reject_credentials_and_secret_paths(self):
        with self.assertRaises(module.MemoryError):
            self.memory.remember({
                "project": "Pilot",
                "operation": "upsert",
                "kind": "constraint",
                "stable_key": "bad",
                "title": "Bad",
                "statement": "password" + "=fixture-value",
                "reason": "unsafe",
                "scope": "test",
                "severity": "high",
                "conditions": [],
            })
        with self.assertRaises(module.MemoryError):
            self.memory.remember({
                "project": "Pilot",
                "operation": "upsert",
                "kind": "environment",
                "stable_key": "watch-secret",
                "title": "Bad watch",
                "fact": "configuration exists",
                "scope": "local",
                "source": "repository",
                "confidence": "high",
                "watch_paths": [".env"],
            })

    def test_candidate_cannot_be_promoted_without_explicit_verification(self):
        values = {
            "project": "Pilot",
            "operation": "occurrence",
            "stable_key": "needs-verification",
            "problem": "fixture needs recovery",
            "context": "fixture",
            "action": ["restart fixture"],
            "applicability": {"scope": "fixture", "versions": ["v1"], "devices": []},
            "constraints": [],
            "warnings": [],
            "verification": ["fixture ready"],
            "outcome": "ready",
        }
        self.memory.remember(values)
        candidate = self.memory.remember(values)
        with self.assertRaises(module.MemoryError):
            self.memory.remember({
                "project": "Pilot",
                "operation": "upsert",
                "kind": "solution",
                "record_id": candidate["id"],
                "stable_key": "needs-verification",
            })

    def test_stale_feedback_updates_record_and_blocks_direct_action(self):
        solution = self.remember_solution("feedback-stale", "feedback stale procedure")
        self.memory.mark_used({"project": "Pilot", "record_id": solution["id"], "outcome": "stale"})
        result = self.memory.recall({"project": "Pilot", "query": "feedback-stale"})
        self.assertIsNone(result["direct_action"])
        self.assertEqual(result["reason"], "stale")

    def test_git_watch_uses_file_blob_not_unrelated_head_change(self):
        watched = self.project / "watched.txt"
        watched.write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "watched.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "fixture"], check=True)
        record = self.memory.remember({
            "project": "Pilot",
            "operation": "upsert",
            "kind": "constraint",
            "stable_key": "watched-constraint",
            "title": "Watched constraint",
            "statement": "Keep the watched behavior",
            "reason": "Tests depend on it",
            "scope": "watched.txt",
            "severity": "high",
            "conditions": [],
            "confidence": "high",
            "verified": True,
            "watch_paths": ["watched.txt"],
        })
        self.assertEqual(self.memory.read({"project": "Pilot", "record_id": record["id"], "view": "card"})["staleness_state"], "fresh")
        unrelated = self.project / "unrelated.txt"
        unrelated.write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "unrelated.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "unrelated"], check=True)
        self.assertEqual(self.memory.read({"project": "Pilot", "record_id": record["id"], "view": "card"})["staleness_state"], "fresh")
        watched.write_text("v2\n", encoding="utf-8")
        self.assertEqual(self.memory.read({"project": "Pilot", "record_id": record["id"], "view": "card"})["staleness_state"], "possibly_stale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
