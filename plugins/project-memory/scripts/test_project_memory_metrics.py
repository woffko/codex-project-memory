#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import sqlite3
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

    def test_same_root_projects_have_separate_metrics_and_child_aggregation(self):
        gui = self.memory.enroll(
            str(self.project),
            "Pilot GUI",
            project_key="pilot/gui",
            parent_project="pilot",
        )
        module.call_tool(
            self.memory,
            "project_memory_status",
            {"project_root": str(self.project)},
        )
        module.call_tool(self.memory, "project_memory_search", {"project": "pilot/gui", "query": "missing"})

        default_only = self.memory.metrics_report(
            project_root=str(self.project),
            since_days=30,
        )
        self.assertEqual([row["project"] for row in default_only["projects"]], ["pilot"])
        self.assertEqual(default_only["projects"][0]["probes"], 1)

        aggregated = self.memory.metrics_report(
            project="pilot",
            include_children=True,
            since_days=30,
        )
        rows = {row["project"]: row for row in aggregated["projects"]}
        self.assertEqual(set(rows), {"pilot", "pilot/gui"})
        self.assertEqual(rows["pilot/gui"]["reads"], 1)
        self.assertNotEqual(gui["project_id"], self.entry["project_id"])

        all_projects = self.memory.metrics_report(all_projects=True, since_days=30)
        self.assertEqual(
            {row["project"] for row in all_projects["projects"]},
            {"pilot", "pilot/gui"},
        )

    def test_structured_error_keeps_clues_but_not_user_content(self):
        credential = "password" + "=" + "fixture-value-never-store"
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

    def test_adaptive_cost_counters_are_aggregate_only(self):
        solution_args = {
            "project": "pilot",
            "operation": "occurrence",
            "stable_key": "verified-queue",
            "problem": "queue stalls",
            "context": "fixture",
            "action": ["restart queue"],
            "applicability": {"scope": "fixture", "versions": ["v1"], "devices": []},
            "constraints": [],
            "warnings": [],
            "verification": ["traffic passed"],
            "outcome": "stable",
        }
        self.call("project_memory_remember", **{key: value for key, value in solution_args.items() if key != "project"})
        solution = self.call("project_memory_remember", **{**{key: value for key, value in solution_args.items() if key != "project"}, "verified": True})
        self.call("project_memory_recall", query="verified-queue")
        self.call("project_memory_read", record_id=solution["id"], view="full")
        usage = self.memory.metrics_report(project="pilot", since_days=30)["projects"][0]
        self.assertEqual(usage["recall_calls"], 1)
        self.assertEqual(usage["followup_read_calls"], 1)
        self.assertEqual(usage["full_reads"], 1)
        self.assertGreater(usage["request_argument_bytes"], 0)
        self.assertGreater(usage["response_bytes"], 0)
        metrics_bytes = b"".join(path.read_bytes() for path in self.memory.metrics.path.parent.glob("usage.sqlite3*"))
        self.assertNotIn(solution["id"].encode(), metrics_bytes)
        self.assertNotIn(b"verified-queue", metrics_bytes)

    def test_metrics_schema_one_migrates_without_losing_aggregates(self):
        path = Path(self.temporary.name) / "legacy-usage.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE metrics_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metrics_metadata VALUES('schema_version','1');
            CREATE TABLE usage_daily(
                day TEXT NOT NULL,project_id TEXT NOT NULL,tool TEXT NOT NULL,operation TEXT NOT NULL,
                success INTEGER NOT NULL,calls INTEGER NOT NULL DEFAULT 0,result_items INTEGER NOT NULL DEFAULT 0,
                hits INTEGER NOT NULL DEFAULT 0,total_duration_us INTEGER NOT NULL DEFAULT 0,
                first_call_at TEXT NOT NULL,last_call_at TEXT NOT NULL,
                PRIMARY KEY(day,project_id,tool,operation,success)
            );
            CREATE TABLE usage_runs(day TEXT NOT NULL,project_id TEXT NOT NULL,server_run_id TEXT NOT NULL,calls INTEGER NOT NULL DEFAULT 0,first_call_at TEXT NOT NULL,last_call_at TEXT NOT NULL,PRIMARY KEY(day,project_id,server_run_id));
            CREATE TABLE error_events(error_id TEXT PRIMARY KEY,occurred_at TEXT NOT NULL,project_id TEXT NOT NULL,tool TEXT NOT NULL,operation TEXT NOT NULL,category TEXT NOT NULL,phase TEXT NOT NULL,error_code TEXT NOT NULL,exception_type TEXT NOT NULL,system_code TEXT,fingerprint TEXT NOT NULL,server_version TEXT NOT NULL);
            """
        )
        today = module.utc_now()
        connection.execute(
            "INSERT INTO usage_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (today[:10], "legacy", "project_memory_search", "read", 1, 2, 1, 1, 1000, today, today),
        )
        connection.commit()
        connection.close()
        store = module.MetricsStore(path, module.SERVER_VERSION)
        store.record_call(project_id="legacy", tool="project_memory_recall", operation="recall", success=True, duration_us=10, result_items=1, response_bytes=120, response_details={"estimated_tokens": 40, "cards_returned": 1})
        row = store.summary(["legacy"], 30)[0]
        self.assertEqual(row["total_calls"], 3)
        self.assertEqual(row["recall_calls"], 1)
        self.assertEqual(row["response_bytes"], 120)
        self.assertIsNotNone(store.last_migration_backup)
        self.assertTrue(Path(store.last_migration_backup).exists())
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
