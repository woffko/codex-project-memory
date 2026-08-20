#!/usr/bin/env python3
"""Local quality-and-cost benchmark for Project Memory recall modes."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import tempfile
from pathlib import Path
from typing import Any

from project_memory_mcp import ProjectMemory, compact_json, utc_now


MODES = ("legacy", "compact", "balanced", "auto", "deep")


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def returned_ids(result: dict[str, Any]) -> list[str]:
    values: list[str] = []
    direct = result.get("direct_action")
    if isinstance(direct, dict) and isinstance(direct.get("record"), dict):
        values.append(str(direct["record"].get("id", "")))
    for section in ("cards", "alternatives"):
        values.extend(str(item.get("id", "")) for item in result.get(section, []) if isinstance(item, dict))
    return [value for value in values if value]


def contains_all(result: dict[str, Any], values: list[str]) -> bool:
    rendered = compact_json(result).casefold()
    return all(value.casefold() in rendered for value in values)


def legacy_recall(memory: ProjectMemory, project: str, query: str) -> tuple[dict[str, Any], int]:
    searched = memory.search({"project": project, "query": query, "limit": 8})
    calls = 1
    if searched["results"]:
        record = memory.get({"project": project, "record_id": searched["results"][0]["id"]})
        calls += 1
        return {"direct_action": {"record": record}, "cards": searched["results"][1:4]}, calls
    return {"direct_action": None, "cards": []}, calls


def seed_fixture(document: dict[str, Any]) -> tuple[tempfile.TemporaryDirectory[str], ProjectMemory, str, dict[str, str]]:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    project_root = root / "project"
    project_root.mkdir()
    memory = ProjectMemory(root / "data", root / "config", profile="admin")
    project = str(document.get("project", "Benchmark"))
    memory.enroll(str(project_root), project_key=project)
    identifiers: dict[str, str] = {}
    deferred: list[dict[str, Any]] = []
    for record in document.get("records", []):
        value = dict(record)
        directives = {key: value.pop(key) for key in list(value) if key.startswith("_")}
        kind = value.get("kind")
        stable_key = value.get("stable_key")
        if kind == "solution":
            occurrence = {**value, "project": project, "operation": "occurrence"}
            occurrence.pop("kind", None)
            memory.remember(occurrence)
            result = memory.remember({**occurrence, "verified": True})
        elif kind == "incomplete_solution":
            connection = memory._connect(memory.resolve_project_key(project))
            record_id = str(value.get("id") or stable_key)
            now = utc_now()
            connection.execute(
                """INSERT INTO records(id,kind,title,summary,problem,context,payload_json,importance,
                           confidence,stable_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, "solution", value.get("title", stable_key), value.get("summary", ""), value.get("problem", ""), value.get("context", ""), compact_json(value.get("payload", {})), "durable", "medium", stable_key, now, now),
            )
            memory._refresh_record(connection, record_id)
            connection.commit()
            connection.close()
            result = {"id": record_id}
        else:
            operation = "checkpoint" if kind == "checkpoint" else "upsert"
            result = memory.remember({**value, "project": project, "operation": operation})
        if stable_key:
            identifiers[str(stable_key)] = str(result["id"])
        deferred.append({"record": record, "directives": directives})
    for item in deferred:
        record = item["record"]
        directives = item["directives"]
        stable_key = record.get("stable_key")
        record_id = identifiers.get(str(stable_key))
        if not record_id:
            continue
        for index in range(int(directives.get("_ordinary_failures", 0))):
            connection = memory._connect(memory.resolve_project_key(project))
            memory._insert_event(connection, record_id, "failed_attempt", "ordinary", f"ordinary attempt {index}", {"attempt": f"attempt {index}", "observed_result": "did not fix it", "why_it_failed": "unknown", "do_not_repeat": ""})
            memory._refresh_record(connection, record_id)
            connection.commit()
            connection.close()
        target_key = directives.get("_contradicts")
        if target_key:
            connection = memory._connect(memory.resolve_project_key(project))
            connection.execute("INSERT INTO record_relations(source_id,relation,target_id,created_at) VALUES(?,?,?,?)", (record_id, "contradicts", identifiers[str(target_key)], utc_now()))
            connection.commit()
            connection.close()
        supersedes_key = directives.get("_supersedes")
        if supersedes_key:
            memory.remember({"project": project, "operation": "upsert", "kind": record["kind"], "stable_key": stable_key, "record_id": record_id, "supersedes_id": identifiers[str(supersedes_key)]})
        stale_state = directives.get("_stale_state")
        if stale_state:
            memory.report_stale({"project": project, "record_id": record_id, "state": stale_state, "reason": "benchmark fixture"})
    return temporary, memory, project, identifiers


def run_benchmark(memory: ProjectMemory, project: str, cases: list[dict[str, Any]], modes: list[str], identifiers: dict[str, str]) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for mode in modes:
        rows: list[dict[str, Any]] = []
        for case in cases:
            query = str(case["query"])
            if mode == "legacy":
                result, calls = legacy_recall(memory, project, query)
            else:
                result = memory.recall({
                    "project": project,
                    "query": query,
                    "mode": mode,
                    "task_type": case.get("task_type", ""),
                    "risk": case.get("risk", "normal"),
                })
                calls = 1
            ids = returned_ids(result)
            expected = [identifiers.get(value, value) for value in case.get("expected_stable_keys", case.get("expected_record_ids", []))]
            expected_direct = bool(case.get("expected_direct_action", False))
            no_result = bool(case.get("expected_no_result", False))
            coverage = contains_all(result, case.get("required_constraints", [])) and contains_all(result, case.get("required_verification", []))
            hit1 = bool(expected and ids and ids[0] in expected)
            hit3 = bool(expected and any(value in expected for value in ids[:3]))
            direct = result.get("direct_action") is not None
            success = (not ids if no_result else hit3) and coverage and (not expected_direct or direct)
            byte_count = len(compact_json(result).encode("utf-8"))
            rows.append({
                "name": case.get("name", query[:80]),
                "success": success,
                "hit_at_1": hit1,
                "hit_at_3": hit3,
                "direct": direct,
                "coverage": coverage,
                "bytes": byte_count,
                "estimated_tokens": math.ceil(byte_count / 3),
                "tool_calls": calls,
            })
        successful = sum(int(row["success"]) for row in rows)
        byte_values = [row["bytes"] for row in rows]
        reports[mode] = {
            "cases": len(rows),
            "correct_task_completion_rate": round(successful / len(rows), 4) if rows else 0,
            "hit_at_1": round(sum(int(row["hit_at_1"]) for row in rows) / len(rows), 4) if rows else 0,
            "hit_at_3": round(sum(int(row["hit_at_3"]) for row in rows) / len(rows), 4) if rows else 0,
            "direct_solution_rate": round(sum(int(row["direct"]) for row in rows) / len(rows), 4) if rows else 0,
            "mandatory_field_coverage": round(sum(int(row["coverage"]) for row in rows) / len(rows), 4) if rows else 0,
            "average_response_bytes": round(statistics.mean(byte_values), 1) if rows else 0,
            "p95_response_bytes": percentile(byte_values, 0.95),
            "average_estimated_tokens": round(statistics.mean(row["estimated_tokens"] for row in rows), 1) if rows else 0,
            "average_tool_calls": round(statistics.mean(row["tool_calls"] for row in rows), 3) if rows else 0,
            "tokens_per_successful_task": round(sum(row["estimated_tokens"] for row in rows if row["success"]) / successful, 1) if successful else None,
            "unsuccessful_cases": len(rows) - successful,
            "details": rows,
        }
    return {"project": project, "modes": reports}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="Enrolled project key; optional when the query file contains fixture records")
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--compare", default=",".join(MODES))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    document = json.loads(args.queries.read_text(encoding="utf-8"))
    cases = document["cases"] if isinstance(document, dict) else document
    modes = [value.strip() for value in args.compare.split(",") if value.strip()]
    if not modes or any(mode not in MODES for mode in modes):
        parser.error(f"--compare must contain only: {', '.join(MODES)}")
    temporary = None
    if isinstance(document, dict) and document.get("records"):
        temporary, memory, project, identifiers = seed_fixture(document)
    else:
        if not args.project:
            parser.error("--project is required when the query file has no fixture records")
        memory = ProjectMemory()
        project = args.project
        identifiers = {}
    try:
        report = run_benchmark(memory, project, cases, modes, identifiers)
    finally:
        memory.metrics.close()
        if temporary is not None:
            temporary.cleanup()
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
