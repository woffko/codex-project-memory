#!/usr/bin/env python3
"""Deterministic, secret-free model-facing views for Project Memory records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


VIEW_SCHEMA_VERSION = 1
IMPORTANCE_VALUES = {"pinned", "durable", "working", "cold"}
CONFIDENCE_VALUES = {"low", "medium", "high", "verified"}
STALE_STATES = {"fresh", "unknown", "possibly_stale", "stale", "superseded", "expired"}
RECORD_KINDS = {
    "candidate",
    "solution",
    "constraint",
    "decision",
    "failure_pattern",
    "environment",
    "checkpoint",
    "log_location",
    "test_asset",
}


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def serialized_bytes(value: Any) -> int:
    return len(compact_json(value).encode("utf-8"))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _deduplicate(items: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        key = compact_json(item).casefold() if isinstance(item, (dict, list)) else str(item).casefold().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _present(payload: dict[str, Any], *names: str) -> bool:
    return any(name in payload for name in names)


def _versions_or_devices(payload: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for key in ("versions", "devices", "platforms"):
        values.extend(_items(payload.get(key)))
    applicability = payload.get("applicability")
    if isinstance(applicability, dict):
        for key in ("versions", "devices", "platforms"):
            values.extend(_items(applicability.get(key)))
    return _deduplicate(values)


def _solution_fields(record: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    steps = _deduplicate(
        _items(payload.get("steps"))
        or _items(payload.get("final_steps"))
        or _items(payload.get("action"))
    )
    constraints = _deduplicate(_items(payload.get("constraints")))
    warnings = _deduplicate(_items(payload.get("warnings")))
    verification = _deduplicate(_items(payload.get("verification")))
    versions_or_devices = _versions_or_devices(payload)
    applicability = payload.get("applicability", "")
    fields = {
        "problem": _text(record.get("problem")) or _text(payload.get("problem")),
        "applicability": applicability,
        "context": _text(record.get("context")) or _text(payload.get("context")),
        "steps": steps,
        "constraints": constraints,
        "warnings": warnings,
        "verification": verification,
        "outcome": _text(payload.get("outcome")) or _text(record.get("summary")),
        "versions_or_devices": versions_or_devices,
    }
    completeness = {
        "problem": bool(fields["problem"]),
        "applicability": _present(payload, "applicability") and applicability not in (None, "", {}, []),
        "context": bool(fields["context"]),
        "steps": bool(steps),
        "constraints": _present(payload, "constraints"),
        "warnings": _present(payload, "warnings"),
        "verification": bool(verification),
        "outcome": bool(fields["outcome"]),
        "versions_or_devices": _present(payload, "versions", "devices", "platforms")
        or (isinstance(applicability, dict) and any(key in applicability for key in ("versions", "devices", "platforms"))),
        "provenance": bool(record.get("provenance")),
    }
    return fields, completeness


def _kind_fields(kind: str, record: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    if kind in {"solution", "candidate"}:
        return _solution_fields(record, payload)
    definitions = {
        "constraint": ("statement", "reason", "scope", "severity", "conditions"),
        "decision": (
            "decision",
            "reason",
            "alternatives_considered",
            "rejected_reasons",
            "consequences",
            "revisit_when",
        ),
        "failure_pattern": ("symptom", "attempt", "observed_result", "why_it_failed", "do_not_repeat"),
        "environment": ("fact", "scope", "source", "confidence"),
        "checkpoint": (
            "goal",
            "completed",
            "current_state",
            "next_steps",
            "blockers",
            "branch",
            "head_commit",
            "expires_at",
        ),
    }
    names = definitions.get(kind, ())
    fields = {name: payload.get(name) for name in names if name in payload}
    completeness = {name: name in payload and payload.get(name) not in (None, "") for name in names}
    return fields, completeness


def required_fields(kind: str) -> tuple[str, ...]:
    return {
        "solution": (
            "problem",
            "applicability",
            "context",
            "steps",
            "constraints",
            "warnings",
            "verification",
            "outcome",
            "versions_or_devices",
        ),
        "constraint": ("statement", "reason", "scope", "severity", "conditions"),
        "decision": (
            "decision",
            "reason",
            "alternatives_considered",
            "rejected_reasons",
            "consequences",
            "revisit_when",
        ),
        "failure_pattern": ("symptom", "attempt", "observed_result", "why_it_failed", "do_not_repeat"),
        "environment": ("fact", "scope", "source", "confidence"),
        "checkpoint": (
            "goal",
            "completed",
            "current_state",
            "next_steps",
            "blockers",
            "branch",
            "head_commit",
            "expires_at",
        ),
    }.get(kind, ())


def is_complete(kind: str, completeness: dict[str, bool]) -> bool:
    required = required_fields(kind)
    return bool(required) and all(completeness.get(field, False) for field in required)


@dataclass(frozen=True)
class MaterializedViews:
    card: dict[str, Any]
    action: dict[str, Any]
    evidence: dict[str, Any]
    completeness: dict[str, bool]
    card_bytes: int
    action_bytes: int
    evidence_bytes: int


def build_record_views(
    record: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    maximum_important_failures: int = 3,
) -> MaterializedViews:
    """Build bounded hot views without modifying the canonical payload."""
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    kind = str(record.get("kind") or "")
    fields, completeness = _kind_fields(kind, record, payload)
    stale_state = str(record.get("stale_state") or "unknown")
    if stale_state not in STALE_STATES:
        stale_state = "unknown"
    confidence = str(record.get("confidence") or "medium")
    if confidence not in CONFIDENCE_VALUES:
        confidence = "medium"
    importance = str(record.get("importance") or "durable")
    if importance not in IMPORTANCE_VALUES:
        importance = "durable"
    summary = _text(record.get("summary")) or _text(record.get("problem")) or _text(record.get("title"))
    applicability = payload.get("applicability")
    applicability_hint = ""
    if isinstance(applicability, str):
        applicability_hint = applicability
    elif isinstance(applicability, dict):
        applicability_hint = compact_json(applicability)
    elif _text(record.get("context")):
        applicability_hint = _text(record.get("context"))
    card = {
        "id": record.get("id"),
        "kind": kind,
        "title": record.get("title", ""),
        "summary": summary,
        "applicability_hint": applicability_hint,
        "confidence": confidence,
        "importance": importance,
        "scope": payload.get("scope", record.get("context", "")),
        "match_reason": [],
        "staleness_state": stale_state,
    }
    if record.get("verified_at"):
        card["last_verified_at"] = record["verified_at"]
    if record.get("supersedes_id"):
        card["supersedes_hint"] = record["supersedes_id"]

    action = {
        "id": record.get("id"),
        "kind": kind,
        "title": record.get("title", ""),
        **fields,
        "confidence": confidence,
        "importance": importance,
        "last_verified_at": record.get("verified_at"),
        "staleness_state": stale_state,
    }
    action = {key: value for key, value in action.items() if value not in (None, "")}
    coverage = dict(completeness)
    important_failures: list[dict[str, Any]] = []
    ordinary_failures = 0
    occurrence_events = 0
    for event in events:
        event_type = event.get("event_type")
        if event_type == "occurrence":
            occurrence_events += 1
        if event_type != "failed_attempt":
            continue
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        if event.get("importance") in {"important", "critical"}:
            if len(important_failures) < maximum_important_failures:
                important_failures.append(detail or {"summary": event.get("summary", "")})
        else:
            ordinary_failures += 1
    evidence = {
        **action,
        "important_failures": important_failures,
        "rationale": payload.get("rationale", payload.get("reason", "")),
        "alternatives": payload.get("alternatives_considered", []),
        "provenance": record.get("provenance") or {},
        "coverage": {**coverage, "important_failures": bool(important_failures), "full_history": False},
        "more_available": bool(events),
        "omitted": {
            "occurrence_events": occurrence_events,
            "ordinary_failures": ordinary_failures,
        },
    }
    evidence = {key: value for key, value in evidence.items() if value not in (None, "", [], {}) or key in {"coverage", "more_available", "omitted"}}
    return MaterializedViews(
        card=card,
        action=action,
        evidence=evidence,
        completeness=completeness,
        card_bytes=serialized_bytes(card),
        action_bytes=serialized_bytes(action),
        evidence_bytes=serialized_bytes(evidence),
    )
