#!/usr/bin/env python3
"""Local privacy-preserving usage metrics for Project Memory."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


METRICS_SCHEMA_VERSION = 1
ERROR_RETENTION_DAYS = 90
UNKNOWN_PROJECT_ID = ""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def metrics_enabled_from_environment() -> bool:
    value = os.environ.get("PROJECT_MEMORY_METRICS", "1").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def validate_since_days(value: int) -> int:
    if value < 1 or value > 36_500:
        raise ValueError("since_days must be between 1 and 36500")
    return value


def cutoff_day(since_days: int) -> str:
    days = validate_since_days(since_days)
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days - 1)).date().isoformat()


def error_fingerprint(
    server_version: str,
    tool: str,
    operation: str,
    exception_type: str,
    phase: str,
    error_code: str,
    system_code: str | None,
) -> str:
    source = "|".join(
        (server_version, tool, operation, exception_type, phase, error_code, system_code or "")
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class MetricsStore:
    """Collect aggregate calls and sanitized errors outside project content DBs."""

    def __init__(
        self,
        path: Path,
        server_version: str,
        *,
        enabled: bool | None = None,
        server_run_id: str | None = None,
    ):
        self.path = path
        self.server_version = server_version
        self.enabled = metrics_enabled_from_environment() if enabled is None else enabled
        self.server_run_id = server_run_id or str(uuid.uuid4())
        self._initialized = False
        self._pruned_errors = False
        self._writer: sqlite3.Connection | None = None

    def _secure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)

    def _write_connection(self) -> sqlite3.Connection:
        if self._writer is not None:
            return self._writer
        self._secure_parent()
        connection = sqlite3.connect(self.path, timeout=2.0)
        try:
            self.path.chmod(0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=2000")
            if not self._initialized:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=256")
                connection.execute("PRAGMA journal_size_limit=1048576")
                self._initialize(connection)
                self._initialized = True
            connection.execute("PRAGMA synchronous=NORMAL")
            self._writer = connection
            return connection
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._initialized = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _read_connection(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        uri = f"file:{quote(self.path.resolve().as_posix(), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            row = connection.execute(
                "SELECT value FROM metrics_metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None or int(row["value"]) != METRICS_SCHEMA_VERSION:
                raise sqlite3.DatabaseError("unsupported usage metrics schema")
            return connection
        except Exception:
            connection.close()
            raise

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metrics_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_daily (
                day TEXT NOT NULL,
                project_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                operation TEXT NOT NULL,
                success INTEGER NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                result_items INTEGER NOT NULL DEFAULT 0,
                hits INTEGER NOT NULL DEFAULT 0,
                total_duration_us INTEGER NOT NULL DEFAULT 0,
                first_call_at TEXT NOT NULL,
                last_call_at TEXT NOT NULL,
                PRIMARY KEY(day, project_id, tool, operation, success)
            );
            CREATE TABLE IF NOT EXISTS usage_runs (
                day TEXT NOT NULL,
                project_id TEXT NOT NULL,
                server_run_id TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                first_call_at TEXT NOT NULL,
                last_call_at TEXT NOT NULL,
                PRIMARY KEY(day, project_id, server_run_id)
            );
            CREATE TABLE IF NOT EXISTS error_events (
                error_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                project_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                operation TEXT NOT NULL,
                category TEXT NOT NULL,
                phase TEXT NOT NULL,
                error_code TEXT NOT NULL,
                exception_type TEXT NOT NULL,
                system_code TEXT,
                fingerprint TEXT NOT NULL,
                server_version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS error_events_project_time_idx
                ON error_events(project_id, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS error_events_code_time_idx
                ON error_events(error_code, occurred_at DESC);
            """
        )
        row = connection.execute(
            "SELECT value FROM metrics_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is not None and int(row["value"]) != METRICS_SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"usage metrics schema {row['value']} is not supported by schema {METRICS_SCHEMA_VERSION}"
            )
        connection.execute(
            "INSERT INTO metrics_metadata(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(METRICS_SCHEMA_VERSION),),
        )
        connection.commit()

    def record_call(
        self,
        *,
        project_id: str | None,
        tool: str,
        operation: str,
        success: bool,
        duration_us: int,
        result_items: int = 0,
        error: dict[str, str | None] | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        now = utc_now()
        day = now[:10]
        normalized_project_id = project_id or UNKNOWN_PROJECT_ID
        error_id = str(uuid.uuid4()) if error is not None else None
        hit = int(tool == "project_memory_search" and success and result_items > 0)
        connection = self._write_connection()
        try:
            connection.execute(
                """INSERT INTO usage_daily(
                       day,project_id,tool,operation,success,calls,result_items,hits,
                       total_duration_us,first_call_at,last_call_at
                   ) VALUES(?,?,?,?,?,1,?,?,?,?,?)
                   ON CONFLICT(day,project_id,tool,operation,success) DO UPDATE SET
                       calls=usage_daily.calls+1,
                       result_items=usage_daily.result_items+excluded.result_items,
                       hits=usage_daily.hits+excluded.hits,
                       total_duration_us=usage_daily.total_duration_us+excluded.total_duration_us,
                       first_call_at=min(usage_daily.first_call_at,excluded.first_call_at),
                       last_call_at=max(usage_daily.last_call_at,excluded.last_call_at)""",
                (
                    day,
                    normalized_project_id,
                    tool,
                    operation,
                    int(success),
                    max(0, int(result_items)),
                    hit,
                    max(0, int(duration_us)),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO usage_runs(
                       day,project_id,server_run_id,calls,first_call_at,last_call_at
                   ) VALUES(?,?,?,1,?,?)
                   ON CONFLICT(day,project_id,server_run_id) DO UPDATE SET
                       calls=usage_runs.calls+1,
                       first_call_at=min(usage_runs.first_call_at,excluded.first_call_at),
                       last_call_at=max(usage_runs.last_call_at,excluded.last_call_at)""",
                (day, normalized_project_id, self.server_run_id, now, now),
            )
            if error is not None and error_id is not None:
                exception_type = str(error.get("exception_type") or "Exception")
                phase = str(error.get("phase") or "tool_call")
                error_code = str(error.get("error_code") or "internal_error")
                system_code = error.get("system_code")
                connection.execute(
                    """INSERT INTO error_events(
                           error_id,occurred_at,project_id,tool,operation,category,phase,
                           error_code,exception_type,system_code,fingerprint,server_version
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        error_id,
                        now,
                        normalized_project_id,
                        tool,
                        operation,
                        str(error.get("category") or "internal"),
                        phase,
                        error_code,
                        exception_type,
                        system_code,
                        error_fingerprint(
                            self.server_version,
                            tool,
                            operation,
                            exception_type,
                            phase,
                            error_code,
                            system_code,
                        ),
                        self.server_version,
                    ),
                )
                if not self._pruned_errors:
                    cutoff = (
                        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ERROR_RETENTION_DAYS)
                    ).replace(microsecond=0).isoformat()
                    connection.execute("DELETE FROM error_events WHERE occurred_at < ?", (cutoff,))
                    self._pruned_errors = True
            connection.commit()
            return error_id
        except Exception:
            connection.rollback()
            self.close()
            raise

    def summary(self, project_ids: Iterable[str], since_days: int = 30) -> list[dict[str, Any]]:
        identifiers = list(dict.fromkeys(project_ids))
        validate_since_days(since_days)
        empty = {
            "total_calls": 0,
            "probes": 0,
            "reads": 0,
            "creates": 0,
            "edits": 0,
            "reuses": 0,
            "helpful": 0,
            "not_applicable": 0,
            "stale": 0,
            "feedback": 0,
            "errors": 0,
            "searches": 0,
            "search_hits": 0,
            "search_result_items": 0,
            "active_days": 0,
            "server_runs": 0,
            "average_duration_ms": 0.0,
            "first_used_at": None,
            "last_used_at": None,
        }
        result = {project_id: {"project_id": project_id, **empty} for project_id in identifiers}
        if not identifiers:
            return []
        connection = self._read_connection()
        if connection is None:
            return list(result.values())
        placeholders = ",".join("?" for _ in identifiers)
        parameters: list[Any] = [cutoff_day(since_days), *identifiers]
        try:
            rows = connection.execute(
                f"""SELECT
                         project_id,
                         sum(calls) AS total_calls,
                         sum(CASE WHEN operation='probe' AND success=1 THEN calls ELSE 0 END) AS probes,
                         sum(CASE WHEN operation IN ('read','sensitive_read') AND success=1 THEN calls ELSE 0 END) AS reads,
                         sum(CASE WHEN operation='create' AND success=1 THEN calls ELSE 0 END) AS creates,
                         sum(CASE WHEN operation='edit' AND success=1 THEN calls ELSE 0 END) AS edits,
                         sum(CASE WHEN operation='reuse' AND success=1 THEN calls ELSE 0 END) AS reuses,
                         sum(CASE WHEN operation='helpful' AND success=1 THEN calls ELSE 0 END) AS helpful,
                         sum(CASE WHEN operation='not_applicable' AND success=1 THEN calls ELSE 0 END) AS not_applicable,
                         sum(CASE WHEN operation='stale' AND success=1 THEN calls ELSE 0 END) AS stale,
                         sum(CASE WHEN operation IN ('helpful','not_applicable','stale') AND success=1 THEN calls ELSE 0 END) AS feedback,
                         sum(CASE WHEN success=0 THEN calls ELSE 0 END) AS errors,
                         sum(CASE WHEN tool='project_memory_search' AND success=1 THEN calls ELSE 0 END) AS searches,
                         sum(CASE WHEN tool='project_memory_search' AND success=1 THEN hits ELSE 0 END) AS search_hits,
                         sum(CASE WHEN tool='project_memory_search' AND success=1 THEN result_items ELSE 0 END) AS search_result_items,
                         count(DISTINCT day) AS active_days,
                         sum(total_duration_us) AS total_duration_us,
                         min(first_call_at) AS first_used_at,
                         max(last_call_at) AS last_used_at
                     FROM usage_daily
                     WHERE day >= ? AND project_id IN ({placeholders})
                     GROUP BY project_id""",
                parameters,
            ).fetchall()
            run_rows = connection.execute(
                f"""SELECT project_id,count(DISTINCT server_run_id) AS server_runs
                     FROM usage_runs
                     WHERE day >= ? AND project_id IN ({placeholders})
                     GROUP BY project_id""",
                parameters,
            ).fetchall()
        finally:
            connection.close()
        runs = {row["project_id"]: int(row["server_runs"]) for row in run_rows}
        for row in rows:
            project_id = row["project_id"]
            calls = int(row["total_calls"] or 0)
            result[project_id].update(
                {
                    "total_calls": calls,
                    "probes": int(row["probes"] or 0),
                    "reads": int(row["reads"] or 0),
                    "creates": int(row["creates"] or 0),
                    "edits": int(row["edits"] or 0),
                    "reuses": int(row["reuses"] or 0),
                    "helpful": int(row["helpful"] or 0),
                    "not_applicable": int(row["not_applicable"] or 0),
                    "stale": int(row["stale"] or 0),
                    "feedback": int(row["feedback"] or 0),
                    "errors": int(row["errors"] or 0),
                    "searches": int(row["searches"] or 0),
                    "search_hits": int(row["search_hits"] or 0),
                    "search_result_items": int(row["search_result_items"] or 0),
                    "active_days": int(row["active_days"] or 0),
                    "server_runs": runs.get(project_id, 0),
                    "average_duration_ms": round(
                        (int(row["total_duration_us"] or 0) / calls / 1000.0) if calls else 0.0,
                        3,
                    ),
                    "first_used_at": row["first_used_at"],
                    "last_used_at": row["last_used_at"],
                }
            )
        return [result[project_id] for project_id in identifiers]

    def error_summary(
        self,
        project_ids: Iterable[str],
        since_days: int = 30,
        *,
        error_code: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        identifiers = list(dict.fromkeys(project_ids))
        validate_since_days(since_days)
        if not identifiers or limit < 1 or limit > 1000:
            return []
        connection = self._read_connection()
        if connection is None:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        filters = ["occurred_at >= ?", f"project_id IN ({placeholders})"]
        parameters: list[Any] = [
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).replace(microsecond=0).isoformat(),
            *identifiers,
        ]
        if error_code:
            filters.append("error_code = ?")
            parameters.append(error_code)
        parameters.append(limit)
        try:
            rows = connection.execute(
                f"""SELECT project_id,error_code,category,phase,tool,operation,
                            exception_type,system_code,fingerprint,count(*) AS count,
                            max(occurred_at) AS last_seen_at
                     FROM error_events
                     WHERE {' AND '.join(filters)}
                     GROUP BY project_id,error_code,category,phase,tool,operation,
                              exception_type,system_code,fingerprint
                     ORDER BY count DESC,last_seen_at DESC
                     LIMIT ?""",
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def error_events(
        self,
        project_ids: Iterable[str],
        since_days: int = 30,
        *,
        error_code: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        identifiers = list(dict.fromkeys(project_ids))
        validate_since_days(since_days)
        if not identifiers or limit < 1 or limit > 1000:
            return []
        connection = self._read_connection()
        if connection is None:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        filters = ["occurred_at >= ?", f"project_id IN ({placeholders})"]
        parameters: list[Any] = [
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).replace(microsecond=0).isoformat(),
            *identifiers,
        ]
        if error_code:
            filters.append("error_code = ?")
            parameters.append(error_code)
        parameters.append(limit)
        try:
            rows = connection.execute(
                f"""SELECT error_id,occurred_at,project_id,tool,operation,category,
                            phase,error_code,exception_type,system_code,fingerprint,server_version
                     FROM error_events
                     WHERE {' AND '.join(filters)}
                     ORDER BY occurred_at DESC
                     LIMIT ?""",
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
