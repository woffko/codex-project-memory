#!/usr/bin/env python3
"""Tests for registry-only routing and the Claude Code plugin packaging."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("project_memory_mcp.py")
SPEC = importlib.util.spec_from_file_location("project_memory_mcp", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PLUGIN_ROOT = MODULE_PATH.parent.parent
REPOSITORY_ROOT = PLUGIN_ROOT.parent.parent


def snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.project = self.workspace / "project"
        (self.project / "src" / "deep").mkdir(parents=True)
        self.data_home = self.base / "data"
        self.memory = MODULE.ProjectMemory(
            self.data_home,
            self.base / "config",
            longrun_state_dir=self.base / "longrun-state",
        )
        self.memory.enroll(str(self.project), "Pilot", True, project_key="pilot")

    def tearDown(self):
        self.temporary.cleanup()

    def run_hook(self, event: dict | str) -> tuple[str, str]:
        raw = event if isinstance(event, str) else json.dumps(event)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(MODULE.sys, "stdin", io.StringIO(raw)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            MODULE.claude_session_start_hook(memory=self.memory)
        return stdout.getvalue(), stderr.getvalue()

    def test_root_and_nested_directory_resolve_to_enrolled_project(self):
        for path in (self.project, self.project / "src" / "deep"):
            route = self.memory.route(str(path))
            self.assertEqual(route["matched_root"], str(self.project))
            self.assertEqual([item["project"] for item in route["projects"]], ["pilot"])
            self.assertTrue(route["projects"][0]["allow_test_secrets"])
            self.assertEqual(route["descendants"], [])
        text = MODULE.route_context(self.memory.route(str(self.project / "src")))
        self.assertIn('Pass project="pilot" on every Project Memory call.', text)
        self.assertIn("encrypted test-only credential storage enabled", text)

    def test_unenrolled_directory_produces_no_context(self):
        outside = self.base / "outside"
        outside.mkdir()
        route = self.memory.route(str(outside))
        self.assertIsNone(route["matched_root"])
        self.assertEqual(route["projects"], [])
        self.assertEqual(route["descendants"], [])
        self.assertEqual(MODULE.route_context(route), "")

    def test_parent_directory_lists_enrolled_descendants_with_limit(self):
        other = self.workspace / "other"
        other.mkdir()
        self.memory.enroll(str(other), project_key="other")
        route = self.memory.route(str(self.workspace))
        self.assertIsNone(route["matched_root"])
        self.assertEqual(
            route["descendants"],
            [
                {"project_root": str(other), "projects": ["other"]},
                {"project_root": str(self.project), "projects": ["pilot"]},
            ],
        )
        limited = self.memory.route(str(self.workspace), descendant_limit=1)
        self.assertEqual(len(limited["descendants"]), 1)
        self.assertEqual(limited["descendants_omitted"], 1)
        self.assertIn("1 more not listed", MODULE.route_context(limited))

    def test_child_directory_reports_parent_and_parent_reports_child(self):
        child = self.project / "rust"
        child.mkdir()
        self.memory.enroll(str(child), parent_root=str(self.project))
        child_route = self.memory.route(str(child))
        self.assertEqual(child_route["matched_root"], str(child))
        self.assertEqual(child_route["projects"][0]["project"], "pilot/rust")
        self.assertEqual(child_route["projects"][0]["parent_project"], "pilot")
        self.assertIn("parent `pilot`", MODULE.route_context(child_route))

        parent_route = self.memory.route(str(self.project))
        self.assertEqual(parent_route["projects"][0]["children"], ["pilot/rust"])
        self.assertEqual(
            parent_route["descendants"],
            [{"project_root": str(child), "projects": ["pilot/rust"]}],
        )

    def test_same_root_projects_are_all_listed_with_default_first(self):
        self.memory.enroll(str(self.project), project_key="pilot/gui", parent_project="pilot")
        route = self.memory.route(str(self.project))
        self.assertEqual([item["project"] for item in route["projects"]], ["pilot", "pilot/gui"])
        self.assertTrue(route["projects"][0]["is_default_for_root"])
        text = MODULE.route_context(route)
        self.assertIn("several logical project memories", text)
        self.assertIn("(default `pilot`)", text)
        self.assertIn("ask before writing", text)
        self.assertNotIn("Pass project=", text)

    def test_route_reads_only_the_registry(self):
        before = snapshot(self.data_home)
        self.memory.route(str(self.project / "src"))
        self.memory.route(str(self.workspace))
        self.assertEqual(snapshot(self.data_home), before)

        empty_home = self.base / "empty-data"
        empty = MODULE.ProjectMemory(empty_home, self.base / "empty-config")
        self.assertEqual(empty.route(str(self.project))["projects"], [])
        self.assertFalse(empty_home.exists())

    def test_claude_hook_emits_additional_context(self):
        stdout, stderr = self.run_hook({
            "session_id": "fixture",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(self.project / "src"),
        })
        self.assertEqual(stderr, "")
        output = json.loads(stdout)
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "SessionStart")
        self.assertIn("`pilot`", specific["additionalContext"])

    def test_claude_hook_is_silent_outside_projects_and_never_raises(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.assertEqual(self.run_hook({"cwd": str(outside)}), ("", ""))

        stdout, stderr = self.run_hook({"cwd": str(self.base / "missing")})
        self.assertEqual(stdout, "")
        self.assertIn("routing unavailable", stderr)
        self.assertNotIn(str(self.base), stderr)

        stdout, stderr = self.run_hook("not json")
        self.assertEqual(stdout, "")
        self.assertIn("routing unavailable", stderr)


class ClaudePackagingTest(unittest.TestCase):
    def test_manifest_overrides_codex_server_and_keeps_hooks_out_of_codex_path(self):
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
        codex_servers = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())["mcpServers"]
        codex_manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], codex_manifest["name"])
        self.assertEqual(manifest["version"].split("+")[0], codex_manifest["version"].split("+")[0])
        # Claude Code loads the plugin-root .mcp.json as well; a same-named
        # manifest entry replaces its relative Codex launch command.
        self.assertEqual(set(manifest["mcpServers"]), set(codex_servers))
        for server in manifest["mcpServers"].values():
            self.assertTrue(any("${CLAUDE_PLUGIN_ROOT}" in arg for arg in server["args"]))
        # Codex discovers hooks/hooks.json automatically, so the Claude hook
        # must live elsewhere and be referenced only by the Claude manifest.
        self.assertFalse((PLUGIN_ROOT / "hooks" / "hooks.json").exists())
        hooks_path = PLUGIN_ROOT / manifest["hooks"]
        hooks = json.loads(hooks_path.read_text())["hooks"]
        command = hooks["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/claude-session-route.sh", command)
        self.assertTrue((PLUGIN_ROOT / "scripts" / "claude-session-route.sh").exists())

    def test_marketplace_lists_plugin_directory(self):
        marketplace = json.loads((REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text())
        entry = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "project-memory")
        self.assertEqual((REPOSITORY_ROOT / entry["source"]).resolve(), PLUGIN_ROOT.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
