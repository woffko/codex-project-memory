"""Host-session state for Project Memory (stdlib only).

The state lives in the private data home under ``sessions/``. It holds only
host session IDs, project keys, counters, timestamps, and the record ID and
stable key of checkpoints written in a session. It never holds queries, tool
arguments, record contents, or credentials.

Session identity, in order of precedence:

1. ``PROJECT_MEMORY_SESSION_ID`` set explicitly for the server process;
2. a pointer that the Claude Code SessionStart hook writes for its host
   process (``pid-<CLAUDE_PID>.json``), which follows ``/clear`` and resume
   even though the MCP server keeps running with its original environment;
3. ``CLAUDE_CODE_SESSION_ID`` from the server environment.

Set ``PROJECT_MEMORY_SESSIONS=0`` to disable all session state and session
provenance.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

SESSION_STATE_SCHEMA_VERSION = 1
SESSION_RETENTION_DAYS = 30
SESSION_PROJECT_LIMIT = 50
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def valid_session_id(value: Any) -> str | None:
    if isinstance(value, str) and SESSION_ID_PATTERN.fullmatch(value):
        return value
    return None


def sessions_enabled(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get("PROJECT_MEMORY_SESSIONS", "1").strip().casefold() not in {"0", "false", "no", "off"}


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionState:
    def __init__(
        self,
        data_home: Path,
        *,
        environ: Mapping[str, str] | None = None,
        parent_pid: int | None = None,
    ):
        self.environ = os.environ if environ is None else environ
        self.root = data_home / "sessions"
        self.enabled = sessions_enabled(self.environ)
        self.parent_pid = os.getppid() if parent_pid is None else parent_pid

    def _secure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._secure_root()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def pointer_path(self, host_pid: int) -> Path:
        return self.root / f"pid-{int(host_pid)}.json"

    def state_path(self, session_id: str) -> Path:
        checked = valid_session_id(session_id)
        if not checked:
            raise ValueError("invalid session id")
        return self.root / f"session-{checked}.json"

    def write_pointer(self, host_pid: int, session_id: str, source: str | None = None) -> bool:
        """Called by the SessionStart hook: map the host process to its current session."""
        checked = valid_session_id(session_id)
        if not self.enabled or not checked or host_pid <= 0:
            return False
        self._write_json(self.pointer_path(host_pid), {
            "schema_version": SESSION_STATE_SCHEMA_VERSION,
            "host_pid": int(host_pid),
            "session_id": checked,
            "source": source if isinstance(source, str) else None,
            "updated_at": utc_now(),
        })
        return True

    def current_session_id(self) -> str | None:
        if not self.enabled:
            return None
        explicit = valid_session_id(self.environ.get("PROJECT_MEMORY_SESSION_ID"))
        if explicit:
            return explicit
        if self.parent_pid > 1:
            pointer = self._read_json(self.pointer_path(self.parent_pid))
            if pointer and pointer.get("host_pid") == self.parent_pid:
                pointed = valid_session_id(pointer.get("session_id"))
                if pointed:
                    return pointed
        return valid_session_id(self.environ.get("CLAUDE_CODE_SESSION_ID"))

    def load(self, session_id: str) -> dict[str, Any] | None:
        if not self.enabled or not valid_session_id(session_id):
            return None
        return self._read_json(self.state_path(session_id))

    def record_use(
        self,
        session_id: str,
        project_key: str,
        *,
        write: bool,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled or not valid_session_id(session_id):
            return
        now = utc_now()
        path = self.state_path(session_id)
        state = self._read_json(path) or {}
        if state.get("session_id") != session_id:
            state = {
                "schema_version": SESSION_STATE_SCHEMA_VERSION,
                "session_id": session_id,
                "created_at": now,
                "projects": {},
            }
        projects = state.setdefault("projects", {})
        usage = projects.get(project_key)
        if not isinstance(usage, dict):
            usage = {"first_used_at": now, "calls": 0, "writes": 0}
        usage["calls"] = int(usage.get("calls", 0)) + 1
        usage["writes"] = int(usage.get("writes", 0)) + int(bool(write))
        usage["last_used_at"] = now
        if checkpoint and checkpoint.get("record_id"):
            usage["last_checkpoint"] = {
                "record_id": str(checkpoint["record_id"])[:100],
                "stable_key": str(checkpoint.get("stable_key") or "")[:500] or None,
                "saved_at": now,
            }
        projects[project_key] = usage
        if len(projects) > SESSION_PROJECT_LIMIT:
            keep = sorted(projects.items(), key=lambda item: str(item[1].get("last_used_at", "")), reverse=True)
            state["projects"] = dict(keep[:SESSION_PROJECT_LIMIT])
        state["updated_at"] = now
        self._write_json(path, state)

    def recent_projects(self, session_id: str) -> list[dict[str, Any]]:
        state = self.load(session_id) or {}
        projects = state.get("projects")
        if not isinstance(projects, dict):
            return []
        items = [
            {"project": key, **value}
            for key, value in projects.items()
            if isinstance(key, str) and isinstance(value, dict)
        ]
        return sorted(items, key=lambda item: str(item.get("last_used_at", "")), reverse=True)

    def prune(self, retention_days: int = SESSION_RETENTION_DAYS) -> int:
        """Remove expired session files and pointers of host processes that exited."""
        if not self.enabled or not self.root.is_dir():
            return 0
        removed = 0
        cutoff = time.time() - retention_days * 86400
        for path in self.root.iterdir():
            try:
                if path.name.startswith("pid-") and path.suffix == ".json":
                    pid_text = path.stem[4:]
                    if not pid_text.isdigit() or not process_alive(int(pid_text)):
                        path.unlink()
                        removed += 1
                elif path.name.startswith("session-") and path.suffix == ".json":
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
            except OSError:
                continue
        return removed


class EnrollmentDeclines:
    """Repository roots for which the user declined the enrollment offer."""

    def __init__(self, data_home: Path):
        self.path = data_home / "route-declined.json"

    def roots(self) -> set[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        declined = value.get("declined") if isinstance(value, dict) else None
        return {root for root in declined if isinstance(root, str)} if isinstance(declined, dict) else set()

    def decline(self, root: str) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        declined = value.get("declined") if isinstance(value, dict) else None
        declined = declined if isinstance(declined, dict) else {}
        declined[root] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"schema_version": 1, "declined": declined}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)
