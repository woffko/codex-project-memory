#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import project_memory_mcp as module
import project_memory_report as report


class ProjectMemoryMetricsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.project.mkdir()
        self.data = root / "data"
        self.config = root / "config"
        self.memory = module.ProjectMemory(self.data, self.config)
        self.entry = self.memory.enroll(
            str(self.project),
            "Pilot Display",
            project_key="pilot",
        )

    def tearDown(self):
        self.memory.metrics.close()
        self.temporary.cleanup()

    def call(self, name: str, **arguments):
        return module.call_tool(self.memory, name, {"project": "pilot", **arguments})

    def test_calls_are_classified_without_counting_the_stats_observer(self):
        self.call("project_memory_status")
        self.call("project_memory_search", query="nothing")
        first = self.call(
            "project_memory_note_repetition",
            problem="fixture queue stalls",
            action="restart fixture queue",
        )
        self.call(
            "project_memory_note_repetition",
            problem="fixture queue stalls",
            action="restart fixture queue",
        )
        self.call(
            "project_memory_mark_used",
            record_id=first["id"],
            outcome="reused",
        )

        before = self.memory.metrics_report(project="pilot", since_days=30)
        observed = module.call_tool(
            self.memory,
            "project_memory_stats",
            {"project": "pilot", "since_days": 30},
        )
        after = self.memory.metrics_report(project="pilot", since_days=30)

        usage = before["projects"][0]
        self.assertEqual(usage["total_calls"], 5)
        self.assertEqual(usage["probes"], 1)
        self.assertEqual(usage["reads"], 1)
        self.assertEqual(usage["creates"], 1)
        self.assertEqual(usage["edits"], 1)
        self.assertEqual(usage["reuses"], 1)
        self.assertEqual(usage["errors"], 0)
        self.assertEqual(usage["searches"], 1)
        self.assertEqual(usage["search_hits"], 0)
        self.assertEqual(usage["active_days"], 1)
        self.assertEqual(usage["server_runs"], 1)
        self.assertEqual(observed["projects"][0]["total_calls"], 5)
        self.assertEqual(after["projects"][0]["total_calls"], 5)

    def test_search_hit_rate_and_child_aggregation_are_per_project(self):
        candidate = self.call(
            "project_memory_note_repetition",
            problem="shared fixture unavailable",
            action="restart shared fixture",
        )
        self.call("project_memory_search", query="shared fixture")
        self.call("project_memory_mark_used", record_id=candidate["id"], outcome="reused")

        child = self.project / "rust"
        child.mkdir()
        self.memory.enroll(str(child), parent_root=str(self.project))
        module.call_tool(
            self.memory,
            "project_memory_search",
            {"project": "pilot/rust", "query": "missing"},
        )

        report_value = self.memory.metrics_report(
            project="pilot",
            include_children=True,
            since_days=30,
        )
        rows = {row["project"]: row for row in report_value["projects"]}
        self.assertEqual(rows["pilot"]["search_hit_rate"], 100.0)
        self.assertEqual(rows["pilot"]["reuses"], 1)
        self.assertEqual(rows["pilot/rust"]["search_hit_rate"], 0.0)
        self.assertEqual(rows["pilot/rust"]["reads"], 1)

    def test_structured_error_keeps_clues_but_not_user_content(self):
        credential = "password=fixture-value-never-store"
        self.call("project_memory_search", query=credential)
        with self.assertRaises(module.ReportedToolError) as captured:
            self.call(
                "project_memory_note_repetition",
                problem="login failure",
                action=credential,
            )

        error = captured.exception
        self.assertEqual(error.detail["error_code"], "sensitive_content_rejected")
        self.assertEqual(error.detail["category"], "security")
        self.assertIsNotNone(error.error_id)
        structured = module.reported_error_result(error)["structuredContent"]["error"]
        self.assertEqual(structured["error_id"], error.error_id)
        self.assertNotIn(credential, json.dumps(structured))

        events = self.memory.metrics.error_events([self.entry["project_id"]], 30)
        self.assertEqual(events[0]["error_code"], "sensitive_content_rejected")
        self.assertEqual(events[0]["tool"], "project_memory_note_repetition")
        metrics_files = list(self.memory.metrics.path.parent.glob("usage.sqlite3*"))
        metrics_bytes = b"".join(path.read_bytes() for path in metrics_files)
        self.assertNotIn(credential.encode(), metrics_bytes)
        self.assertNotIn(str(self.project).encode(), metrics_bytes)

        with patch.dict(
            os.environ,
            {
                "PROJECT_MEMORY_HOME": str(self.data),
                "PROJECT_MEMORY_CONFIG_HOME": str(self.config),
            },
            clear=False,
        ), patch.object(
            report.sys,
            "argv",
            [
                "codex-project-memory-report",
                "errors",
                "--all",
                "--last",
                "1",
                "--format",
                "json",
            ],
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                report.main()
        reported = json.loads(output.getvalue())
        self.assertEqual(reported[0]["error_id"], error.error_id)
        self.assertEqual(len(reported[0]["fingerprint"]), 64)

    def test_metrics_storage_failure_does_not_break_memory_tools(self):
        self.memory.metrics.path = self.data
        result = self.call("project_memory_status")
        self.assertTrue(result["enrolled"])
        self.assertFalse(result["usage_30d"]["available"])

    def test_system_errors_keep_machine_readable_codes(self):
        detail = module.describe_exception(OSError(errno.ENOSPC, "disk full", "/private/path"))
        self.assertEqual(detail["error_code"], "disk_full")
        self.assertEqual(detail["system_code"], "ENOSPC")
        self.assertNotIn("/private/path", json.dumps(detail))

    def test_reporter_reads_metrics_without_modifying_database(self):
        self.call("project_memory_status")
        metrics_path = self.memory.metrics.path
        before_bytes = metrics_path.read_bytes()
        before_mtime = metrics_path.stat().st_mtime_ns
        with patch.dict(
            os.environ,
            {
                "PROJECT_MEMORY_HOME": str(self.data),
                "PROJECT_MEMORY_CONFIG_HOME": str(self.config),
            },
            clear=False,
        ), patch.object(
            report.sys,
            "argv",
            ["codex-project-memory-report", "summary", "--all", "--format", "json"],
        ), patch.object(
            module.ProjectMemory,
            "_connect",
            side_effect=AssertionError("reporter must not open project content DBs"),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                report.main()

        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["projects"][0]["project"], "pilot")
        self.assertEqual(parsed["projects"][0]["probes"], 1)
        self.assertEqual(metrics_path.read_bytes(), before_bytes)
        self.assertEqual(metrics_path.stat().st_mtime_ns, before_mtime)

    def test_metrics_can_be_disabled(self):
        root = Path(self.temporary.name)
        with patch.dict(os.environ, {"PROJECT_MEMORY_METRICS": "0"}, clear=False):
            memory = module.ProjectMemory(root / "disabled-data", root / "disabled-config")
        project = root / "disabled-project"
        project.mkdir()
        memory.enroll(str(project), project_key="disabled")
        module.call_tool(memory, "project_memory_status", {"project": "disabled"})
        self.assertFalse(memory.metrics.enabled)
        self.assertFalse(memory.metrics.path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
