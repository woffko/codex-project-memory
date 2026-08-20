#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

import project_memory_bench as benchmark


class ProjectMemoryBenchmarkTest(unittest.TestCase):
    def test_balanced_fixture_preserves_quality_and_auto_costs_less_than_deep(self):
        document = json.loads(
            (Path(__file__).parents[1] / "testdata" / "recall_cases.json").read_text(encoding="utf-8")
        )
        temporary, memory, project, identifiers = benchmark.seed_fixture(document)
        try:
            result = benchmark.run_benchmark(
                memory,
                project,
                document["cases"],
                ["legacy", "balanced", "auto", "deep"],
                identifiers,
            )["modes"]
        finally:
            memory.metrics.close()
            temporary.cleanup()
        self.assertGreaterEqual(
            result["balanced"]["correct_task_completion_rate"],
            result["legacy"]["correct_task_completion_rate"],
        )
        self.assertEqual(result["auto"]["mandatory_field_coverage"], 1.0)
        self.assertLessEqual(
            result["auto"]["average_estimated_tokens"],
            result["deep"]["average_estimated_tokens"],
        )
        self.assertLess(
            result["auto"]["average_tool_calls"],
            result["legacy"]["average_tool_calls"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
