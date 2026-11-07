#!/usr/bin/env python3
"""Tests for host-session state, session provenance, and the enrollment offer."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = SCRIPTS / "project_memory_mcp.py"
SPEC = importlib.util.spec_from_file_location("project_memory_mcp", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
from project_memory_sessions import SessionState  # noqa: E402

DEAD_PID = 2**22 + 12345  # above the default Linux pid_max, never alive


class SessionStateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_home = Path(self.temporary.name).resolve() / "data"

    def tearDown(self):
        self.temporary.cleanup()

    def state(self, environ: dict[str, str], parent_pid: int = 4242) -> SessionState:
        return SessionState(self.data_home, environ=environ, parent_pid=parent_pid)

    def test_session_id_precedence_explicit_pointer_environment(self):
        writer = self.state({})
        self.assertTrue(writer.write_pointer(4242, "after-clear", "clear"))
        self.assertEqual(self.state({"CLAUDE_CODE_SESSION_ID": "spawned"}).current_session_id(), "after-clear")
        self.assertEqual(
            self.state({"CLAUDE_CODE_SESSION_ID": "spawned", "PROJECT_MEMORY_SESSION_ID": "explicit"}).current_session_id(),
            "explicit",
        )
        self.assertEqual(self.state({"CLAUDE_CODE_SESSION_ID": "spawned"}, parent_pid=777).current_session_id(), "spawned")
        self.assertIsNone(self.state({}, parent_pid=777).current_session_id())
        self.assertIsNone(self.state({"CLAUDE_CODE_SESSION_ID": "../escape"}, parent_pid=777).current_session_id())

    def test_pointer_for_another_host_pid_is_ignored(self):
        pointer = self.state({}).pointer_path(4242)
        self.state({}).write_pointer(4242, "real", "startup")
        value = json.loads(pointer.read_text())
        value["host_pid"] = 9999
        pointer.write_text(json.dumps(value))
        self.assertEqual(self.state({"CLAUDE_CODE_SESSION_ID": "spawned"}).current_session_id(), "spawned")

    def test_disabled_state_has_no_identity_and_writes_nothing(self):
        disabled = self.state({"PROJECT_MEMORY_SESSIONS": "0", "CLAUDE_CODE_SESSION_ID": "spawned"})
        self.assertFalse(disabled.write_pointer(4242, "spawned"))
        self.assertIsNone(disabled.current_session_id())
        disabled.record_use("spawned", "pilot", write=True)
        self.assertFalse(self.data_home.exists())

    def test_record_use_counts_calls_writes_and_last_checkpoint(self):
        state = self.state({})
        state.record_use("s1", "pilot", write=False)
        state.record_use("s1", "pilot", write=True, checkpoint={"record_id": "r1", "stable_key": "goal-a"})
        state.record_use("s1", "other", write=False)
        recent = state.recent_projects("s1")
        self.assertEqual({item["project"] for item in recent}, {"pilot", "other"})
        pilot = next(item for item in recent if item["project"] == "pilot")
        self.assertEqual((pilot["calls"], pilot["writes"]), (2, 1))
        self.assertEqual(pilot["last_checkpoint"]["record_id"], "r1")
        self.assertEqual(pilot["last_checkpoint"]["stable_key"], "goal-a")
        path = state.state_path("s1")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_prune_removes_dead_pointers_and_expired_sessions(self):
        state = self.state({})
        state.write_pointer(os.getpid(), "alive")
        state.write_pointer(DEAD_PID, "dead")
        state.record_use("old", "pilot", write=False)
        state.record_use("fresh", "pilot", write=False)
        expired = time.time() - 40 * 86400
        os.utime(state.state_path("old"), (expired, expired))
        self.assertEqual(state.prune(), 2)
        self.assertTrue(state.pointer_path(os.getpid()).exists())
        self.assertFalse(state.pointer_path(DEAD_PID).exists())
        self.assertFalse(state.state_path("old").exists())
        self.assertTrue(state.state_path("fresh").exists())


class SessionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.data_home = self.base / "data"
        self.memory = MODULE.ProjectMemory(self.data_home, self.base / "config", longrun_state_dir=self.base / "longrun")
        self.memory.enroll(str(self.project), "Pilot", False, project_key="pilot")
        self.use_session({"CLAUDE_CODE_SESSION_ID": "s1"})

    def tearDown(self):
        self.temporary.cleanup()

    def use_session(self, environ: dict[str, str], parent_pid: int = 4242) -> None:
        self.memory.sessions = SessionState(self.data_home, environ=environ, parent_pid=parent_pid)

    def checkpoint_args(self) -> dict:
        return {
            "project": "pilot",
            "operation": "checkpoint",
            "stable_key": "goal-a",
            "goal": "finish goal A",
            "completed": ["schema"],
            "current_state": "ranking untested",
            "next_steps": ["benchmark"],
            "blockers": [],
            "branch": "feature",
            "head_commit": "a1b2c3d",
            "expires_at": "2099-01-01T00:00:00Z",
        }

    def run_hook(self, event: dict, claude_pid: int = 4242) -> tuple[str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(MODULE.sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.dict(os.environ, {"CLAUDE_PID": str(claude_pid)}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            MODULE.claude_session_start_hook(memory=self.memory)
        return stdout.getvalue(), stderr.getvalue()

    def test_tool_calls_record_session_usage_and_audit_provenance(self):
        MODULE.call_tool(self.memory, "project_memory_note_repetition", {"project": "pilot", "problem": "queue stalls", "action": "restart queue"})
        MODULE.call_tool(self.memory, "project_memory_recall", {"project": "pilot", "query": "queue stalls"})
        saved = MODULE.call_tool(self.memory, "project_memory_remember", self.checkpoint_args())

        usage = self.memory.sessions.recent_projects("s1")
        self.assertEqual(len(usage), 1)
        self.assertEqual((usage[0]["project"], usage[0]["calls"], usage[0]["writes"]), ("pilot", 3, 2))
        self.assertEqual(usage[0]["last_checkpoint"]["record_id"], saved["id"])
        self.assertEqual(usage[0]["last_checkpoint"]["stable_key"], "goal-a")

        audit = self.memory.session_audit("pilot", "s1")["events"]
        self.assertEqual(len(audit), 2)
        self.assertTrue(all(event["session_id"] == "s1" for event in audit))
        self.assertEqual(self.memory.session_audit("pilot", "other")["events"], [])

    def test_writes_follow_the_hook_pointer_after_clear(self):
        MODULE.call_tool(self.memory, "project_memory_note_repetition", {"project": "pilot", "problem": "first", "action": "first action"})
        self.run_hook({"session_id": "s2", "source": "clear", "cwd": str(self.project)})
        MODULE.call_tool(self.memory, "project_memory_note_repetition", {"project": "pilot", "problem": "second", "action": "second action"})
        sessions = {event["session_id"] for event in self.memory.session_audit("pilot")["events"]}
        self.assertEqual(sessions, {"s1", "s2"})
        self.assertEqual(self.memory.sessions.recent_projects("s2")[0]["writes"], 1)

    def test_resume_and_compact_recall_session_projects_and_checkpoint(self):
        saved = MODULE.call_tool(self.memory, "project_memory_remember", self.checkpoint_args())
        for source in ("resume", "compact"):
            stdout, stderr = self.run_hook({"session_id": "s1", "source": source, "cwd": str(self.base)})
            self.assertEqual(stderr, "")
            text = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"Project Memory in this session ({source})", text)
            self.assertIn("Keep using `pilot`", text)
            self.assertIn(saved["id"], text)
            self.assertIn("`goal-a`", text)

    def test_startup_and_clear_do_not_replay_session_history(self):
        MODULE.call_tool(self.memory, "project_memory_note_repetition", {"project": "pilot", "problem": "first", "action": "first action"})
        for source in ("startup", "clear"):
            stdout, _ = self.run_hook({"session_id": "s1", "source": source, "cwd": str(self.project)})
            self.assertNotIn("in this session", json.loads(stdout)["hookSpecificOutput"]["additionalContext"])

    def test_hook_writes_pointer_only_with_valid_identity(self):
        self.run_hook({"session_id": "s9", "source": "startup", "cwd": str(self.project)}, claude_pid=5151)
        pointer = json.loads(self.memory.sessions.pointer_path(5151).read_text())
        self.assertEqual((pointer["session_id"], pointer["source"]), ("s9", "startup"))
        self.run_hook({"session_id": "../bad", "source": "startup", "cwd": str(self.project)}, claude_pid=5252)
        self.assertFalse(self.memory.sessions.pointer_path(5252).exists())

    def test_startup_hook_prunes_pointers_of_exited_hosts(self):
        self.memory.sessions.write_pointer(DEAD_PID, "gone")
        self.run_hook({"session_id": "s3", "source": "resume", "cwd": str(self.project)}, claude_pid=os.getpid())
        self.assertTrue(self.memory.sessions.pointer_path(DEAD_PID).exists())
        self.run_hook({"session_id": "s3", "source": "startup", "cwd": str(self.project)}, claude_pid=os.getpid())
        self.assertFalse(self.memory.sessions.pointer_path(DEAD_PID).exists())
        self.assertTrue(self.memory.sessions.pointer_path(os.getpid()).exists())


class EnrollmentOfferTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repository = self.base / "my repo"
        (self.repository / ".git").mkdir(parents=True)
        (self.repository / "src").mkdir()
        self.memory = MODULE.ProjectMemory(self.base / "data", self.base / "config")

    def tearDown(self):
        self.temporary.cleanup()

    def test_unenrolled_repository_is_offered_until_declined(self):
        route = self.memory.route(str(self.repository / "src"))
        self.assertEqual(route["unenrolled_repository"], {"root": str(self.repository), "default_project_key": "my repo"})
        text = MODULE.route_context(route)
        self.assertIn("is not enrolled in Project Memory", text)
        self.assertIn(f"enroll --project-root '{self.repository}' --yes", text)
        self.assertIn(f"route --decline '{self.repository}'", text)

        self.assertEqual(self.memory.decline_enrollment(str(self.repository / "src")), {"declined": str(self.repository)})
        declined = self.memory.route(str(self.repository))
        self.assertIsNone(declined["unenrolled_repository"])
        self.assertEqual(MODULE.route_context(declined), "")

    def test_enrolled_repository_and_home_directory_are_not_offered(self):
        self.memory.enroll(str(self.repository), project_key="repo")
        self.assertIsNone(self.memory.route(str(self.repository / "src"))["unenrolled_repository"])

        other = self.base / "home-repo"
        (other / ".git").mkdir(parents=True)
        (other / "plain").mkdir()
        with patch.object(MODULE.Path, "home", return_value=other):
            self.assertIsNone(self.memory.route(str(other / "plain"))["unenrolled_repository"])

    def test_repository_with_enrolled_children_lists_them_instead_of_offering(self):
        child = self.repository / "src"
        self.memory.enroll(str(child), project_key="repo-src")
        route = self.memory.route(str(self.repository))
        self.assertIsNone(route["unenrolled_repository"])
        self.assertEqual(route["descendants"][0]["projects"], ["repo-src"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
