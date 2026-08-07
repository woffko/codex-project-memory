#!/usr/bin/env python3
"""Read-only local usage and error reports for Project Memory."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from typing import Any, Iterable

from project_memory_mcp import MemoryError, ProjectMemory, describe_exception
from project_memory_metrics import UNKNOWN_PROJECT_ID


def parse_since(value: str) -> int:
    normalized = value.strip().casefold()
    if normalized.endswith("d"):
        normalized = normalized[:-1]
    try:
        days = int(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("period must be a number of days such as 30 or 30d") from exc
    if days < 1 or days > 36_500:
        raise argparse.ArgumentTypeError("period must be between 1d and 36500d")
    return days


def parse_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if limit < 1 or limit > 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return limit


def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--project", help="Hierarchical project key")
    selection.add_argument(
        "--all",
        action="store_true",
        help="Report all enrolled projects; this is the default without --project",
    )
    parser.add_argument(
        "--include-children",
        action="store_true",
        help="Include all descendants of --project",
    )
    parser.add_argument("--since", type=parse_since, default=30, metavar="DAYS")
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table")


def table_text(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "No matching telemetry."
    def cell(row: dict[str, Any], key: str) -> str:
        value = row.get(key, "")
        return "" if value is None else str(value)

    widths = []
    for key, title in columns:
        widths.append(max(len(title), *(len(cell(row, key)) for row in rows)))
    header = "  ".join(title.ljust(width) for (_, title), width in zip(columns, widths))
    divider = "  ".join("-" * width for width in widths)
    lines = [header, divider]
    for row in rows:
        lines.append(
            "  ".join(cell(row, key).ljust(width) for (key, _), width in zip(columns, widths))
        )
    return "\n".join(lines)


def emit_rows(
    rows: list[dict[str, Any]],
    columns: list[tuple[str, str]],
    output_format: str,
    *,
    json_value: Any | None = None,
) -> None:
    if output_format == "json":
        print(json.dumps(rows if json_value is None else json_value, ensure_ascii=False, indent=2))
    elif output_format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=[key for key, _ in columns], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    else:
        print(table_text(rows, columns))


def selected_entries(
    memory: ProjectMemory,
    project: str | None,
    include_children: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if include_children and project is None:
        raise MemoryError(
            "--include-children requires --project",
            code="invalid_argument",
            phase="report",
        )
    return memory._metric_entries(
        project=project,
        all_projects=project is None,
        include_children=include_children,
    )


def summary_command(memory: ProjectMemory, args: argparse.Namespace) -> None:
    if args.include_children and args.project is None:
        raise MemoryError(
            "--include-children requires --project",
            code="invalid_argument",
            phase="report",
        )
    report = memory.metrics_report(
        project=args.project,
        all_projects=args.project is None,
        include_children=args.include_children,
        since_days=args.since,
        error_limit=10,
    )
    rows = []
    for project in report["projects"]:
        rows.append(
            {
                **project,
                "last_used": project["last_used_at"] or "never",
                "hit_rate": f"{project['search_hit_rate']:.1f}%",
            }
        )
    columns = [
        ("project", "PROJECT"),
        ("active_days", "ACTIVE DAYS"),
        ("reads", "READS"),
        ("creates", "CREATES"),
        ("edits", "EDITS"),
        ("reuses", "REUSES"),
        ("feedback", "FEEDBACK"),
        ("errors", "ERRORS"),
        ("hit_rate", "HIT RATE"),
        ("last_used", "LAST USED"),
    ]
    emit_rows(rows, columns, args.format, json_value=report)


def map_project_keys(
    entries: Iterable[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    keys = {entry["project_id"]: entry["project_key"] for entry in entries}
    result = []
    for row in rows:
        mapped = dict(row)
        mapped["project"] = keys.get(mapped.pop("project_id"), "<unresolved>")
        result.append(mapped)
    return result


def errors_command(memory: ProjectMemory, args: argparse.Namespace) -> None:
    _, entries = selected_entries(memory, args.project, args.include_children)
    identifiers = [entry["project_id"] for entry in entries]
    if args.project is None:
        identifiers.append(UNKNOWN_PROJECT_ID)
    if args.last is not None:
        rows = memory.metrics.error_events(
            identifiers,
            args.since,
            error_code=args.error_code,
            limit=args.last,
        )
        rows = map_project_keys(entries, rows)
        if args.format == "table":
            for row in rows:
                row["fingerprint"] = row["fingerprint"][:12]
        columns = [
            ("occurred_at", "OCCURRED"),
            ("project", "PROJECT"),
            ("error_code", "ERROR CODE"),
            ("category", "CATEGORY"),
            ("phase", "PHASE"),
            ("tool", "TOOL"),
            ("exception_type", "EXCEPTION"),
            ("system_code", "SYSTEM CODE"),
            ("error_id", "ERROR ID"),
            ("fingerprint", "FINGERPRINT"),
        ]
    else:
        rows = memory.metrics.error_summary(
            identifiers,
            args.since,
            error_code=args.error_code,
            limit=args.limit,
        )
        rows = map_project_keys(entries, rows)
        if args.format == "table":
            for row in rows:
                row["fingerprint"] = row["fingerprint"][:12]
        columns = [
            ("project", "PROJECT"),
            ("error_code", "ERROR CODE"),
            ("count", "COUNT"),
            ("last_seen_at", "LAST SEEN"),
            ("category", "CATEGORY"),
            ("phase", "PHASE"),
            ("tool", "TOOL"),
            ("exception_type", "EXCEPTION"),
            ("system_code", "SYSTEM CODE"),
            ("fingerprint", "FINGERPRINT"),
        ]
    emit_rows(rows, columns, args.format)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    summary = subparsers.add_parser("summary", help="Show project usage aggregates")
    add_selection_arguments(summary)
    errors = subparsers.add_parser("errors", help="Show sanitized error groups or recent events")
    add_selection_arguments(errors)
    errors.add_argument("--error-code", help="Restrict results to one structured error code")
    errors.add_argument("--limit", type=parse_limit, default=20)
    errors.add_argument("--last", type=parse_limit, metavar="N")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    memory = ProjectMemory()
    try:
        if args.command == "summary":
            summary_command(memory, args)
        else:
            errors_command(memory, args)
    except (MemoryError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        detail = describe_exception(exc)
        print(
            f"project-memory-report: {exc} "
            f"[{detail['error_code']}; phase={detail['phase']}]",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
