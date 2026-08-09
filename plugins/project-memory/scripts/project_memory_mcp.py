#!/usr/bin/env python3
"""Project-scoped operational memory MCP server (stdlib + cryptography)."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from project_memory_metrics import MetricsStore


SERVER_NAME = "Project Memory"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2025-11-25"
REGISTRY_SCHEMA_VERSION = 2
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:password|passwd|pwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+", re.I),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I),
)


class MemoryError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_request",
        category: str = "validation",
        phase: str = "validate",
        system_code: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.category = category
        self.phase = phase
        self.system_code = system_code


class ReportedToolError(Exception):
    def __init__(self, message: str, detail: dict[str, str | None], error_id: str | None):
        super().__init__(message)
        self.detail = detail
        self.error_id = error_id


def describe_exception(exc: Exception) -> dict[str, str | None]:
    if isinstance(exc, MemoryError):
        return {
            "error_code": exc.code,
            "category": exc.category,
            "phase": exc.phase,
            "exception_type": type(exc).__name__,
            "system_code": exc.system_code,
        }
    if isinstance(exc, json.JSONDecodeError):
        return {
            "error_code": "malformed_json",
            "category": "protocol",
            "phase": "decode_request",
            "exception_type": type(exc).__name__,
            "system_code": None,
        }
    if isinstance(exc, InvalidTag):
        return {
            "error_code": "crypto_auth_failed",
            "category": "security",
            "phase": "decrypt",
            "exception_type": type(exc).__name__,
            "system_code": None,
        }
    if isinstance(exc, sqlite3.Error):
        system_code = getattr(exc, "sqlite_errorname", None)
        if system_code in {"SQLITE_BUSY", "SQLITE_LOCKED"}:
            error_code = "sqlite_busy"
        elif system_code in {"SQLITE_CORRUPT", "SQLITE_NOTADB"}:
            error_code = "sqlite_corrupt"
        elif system_code == "SQLITE_FULL":
            error_code = "disk_full"
        elif system_code in {"SQLITE_READONLY", "SQLITE_PERM"}:
            error_code = "permission_denied"
        else:
            error_code = "sqlite_io"
        return {
            "error_code": error_code,
            "category": "storage",
            "phase": "database",
            "exception_type": type(exc).__name__,
            "system_code": system_code,
        }
    if isinstance(exc, OSError):
        system_code = errno.errorcode.get(exc.errno or 0)
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            error_code = "permission_denied"
        elif exc.errno == errno.ENOSPC:
            error_code = "disk_full"
        else:
            error_code = "io_error"
        return {
            "error_code": error_code,
            "category": "storage",
            "phase": "filesystem",
            "exception_type": type(exc).__name__,
            "system_code": system_code,
        }
    if isinstance(exc, (TypeError, ValueError)):
        return {
            "error_code": "invalid_argument",
            "category": "validation",
            "phase": "validate",
            "exception_type": type(exc).__name__,
            "system_code": None,
        }
    return {
        "error_code": "internal_error",
        "category": "internal",
        "phase": "tool_call",
        "exception_type": type(exc).__name__,
        "system_code": None,
    }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def canonical(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=True))


def normalize_project_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryError("project key must be a non-empty string", code="invalid_project_key")
    value = value.strip().replace("\\", "/")
    if len(value) > 1000:
        raise MemoryError("project key exceeds 1000 characters", code="invalid_project_key")
    parts = [part.strip() for part in value.split("/")]
    if any(not part or part in {".", ".."} for part in parts):
        raise MemoryError(
            "project key must contain non-empty path-like components",
            code="invalid_project_key",
        )
    return "/".join(parts)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_fingerprint(*parts: str) -> str:
    text = " ".join(parts).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_sensitive(value: Any, where: str = "value") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            found = find_sensitive(item, f"{where}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = find_sensitive(item, f"{where}[{index}]")
            if found:
                return found
    elif isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                return where
    return None


def require_text(args: dict[str, Any], name: str, limit: int = 20_000) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MemoryError(f"{name} must be a non-empty string", code="invalid_argument")
    value = value.strip()
    if len(value) > limit:
        raise MemoryError(f"{name} exceeds {limit} characters", code="invalid_argument")
    return value


def string_list(value: Any, name: str, limit: int = 50) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise MemoryError(
            f"{name} must be a list with at most {limit} items",
            code="invalid_argument",
        )
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MemoryError(f"{name} items must be non-empty strings", code="invalid_argument")
        result.append(item.strip())
    return result


class ProjectMemory:
    def __init__(self, data_home: Path | None = None, config_home: Path | None = None):
        os.umask(0o077)
        default_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "codex-project-memory"
        default_config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "codex-project-memory"
        self.data_home = data_home or Path(os.environ.get("PROJECT_MEMORY_HOME", default_data_home))
        self.config_home = config_home or Path(os.environ.get("PROJECT_MEMORY_CONFIG_HOME", default_config_home))
        self.registry_path = self.data_home / "registry.json"
        self.metrics = MetricsStore(self.data_home / "usage.sqlite3", SERVER_VERSION)

    def _secure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def _upgrade_registry(self, registry: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(registry, dict) or not isinstance(registry.get("projects", {}), dict):
            raise MemoryError(
                "invalid project-memory registry",
                code="registry_invalid",
                category="configuration",
                phase="registry",
            )
        try:
            version = int(registry.get("schema_version", 1))
        except (TypeError, ValueError) as exc:
            raise MemoryError(
                "invalid project-memory registry schema version",
                code="registry_invalid",
                category="configuration",
                phase="registry",
            ) from exc
        if version > REGISTRY_SCHEMA_VERSION:
            raise MemoryError(
                f"registry schema {version} is newer than supported schema {REGISTRY_SCHEMA_VERSION}",
                code="schema_too_new",
                category="configuration",
                phase="registry",
            )
        projects = registry.setdefault("projects", {})
        used_keys: set[str] = set()
        for root in sorted(projects):
            entry = projects[root]
            if not isinstance(entry, dict):
                raise MemoryError(
                    f"invalid registry entry for project root: {root}",
                    code="registry_invalid",
                    category="configuration",
                    phase="registry",
                )
            candidate = entry.get("project_key") or entry.get("project_name") or Path(root).name or "project"
            try:
                candidate = normalize_project_key(candidate)
            except MemoryError:
                candidate = f"project-{hashlib.sha256(root.encode()).hexdigest()[:8]}"
            if candidate.casefold() in used_keys:
                suffix = hashlib.sha256(root.encode()).hexdigest()
                length = 8
                candidate_base = candidate
                while candidate.casefold() in used_keys:
                    candidate = f"{candidate_base}@{suffix[:length]}"
                    length += 1
            used_keys.add(candidate.casefold())
            entry["project_key"] = candidate
            entry.setdefault("parent_project_id", None)
        registry["schema_version"] = REGISTRY_SCHEMA_VERSION
        return registry

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"schema_version": REGISTRY_SCHEMA_VERSION, "projects": {}}
        try:
            return self._upgrade_registry(json.loads(self.registry_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryError(
                f"cannot read registry: {exc}",
                code="registry_unreadable",
                category="storage",
                phase="registry",
                system_code=errno.errorcode.get(exc.errno or 0) if isinstance(exc, OSError) else None,
            ) from exc

    def _save_registry(self, registry: dict[str, Any]) -> None:
        registry = self._upgrade_registry(registry)
        self._secure_dir(self.data_home)
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.registry_path)
        self.registry_path.chmod(0o600)

    def _entry_by_id(self, registry: dict[str, Any], project_id: str | None) -> dict[str, Any] | None:
        if not project_id:
            return None
        return next((entry for entry in registry["projects"].values() if entry.get("project_id") == project_id), None)

    def _entry_by_key(self, registry: dict[str, Any], project_key: str) -> dict[str, Any] | None:
        folded = normalize_project_key(project_key).casefold()
        return next((entry for entry in registry["projects"].values() if entry["project_key"].casefold() == folded), None)

    def _describe_entry(self, registry: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        parent = self._entry_by_id(registry, entry.get("parent_project_id"))
        children = sorted(
            child["project_key"]
            for child in registry["projects"].values()
            if child.get("parent_project_id") == entry["project_id"]
        )
        return {
            **entry,
            "parent_project": parent["project_key"] if parent else None,
            "children": children,
            "is_meta_project": bool(children),
        }

    def _resolve_root_in_registry(self, registry: dict[str, Any], project_root: str) -> dict[str, Any]:
        try:
            root = canonical(project_root)
        except (OSError, RuntimeError) as exc:
            raise MemoryError(
                f"invalid project root: {exc}",
                code="invalid_project_root",
                category="configuration",
                phase="resolve_project",
            ) from exc
        entry = registry["projects"].get(root)
        if not entry:
            raise MemoryError(
                f"project is not enrolled: {root}",
                code="project_not_enrolled",
                category="configuration",
                phase="resolve_project",
            )
        if entry.get("project_root") != root:
            raise MemoryError(
                "registry project-root mismatch",
                code="registry_root_mismatch",
                category="configuration",
                phase="resolve_project",
            )
        return entry

    def _prepare_enrollment(
        self,
        project_root: str,
        project_name: str | None,
        allow_test_secrets: bool | None,
        parent_root: str | None,
        project_key: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            root = canonical(project_root)
        except (OSError, RuntimeError) as exc:
            raise MemoryError(
                f"invalid project root: {exc}",
                code="invalid_project_root",
                category="configuration",
                phase="enroll",
            ) from exc
        registry = self._load_registry()
        existing = registry["projects"].get(root)
        parent = None
        parent_project_id = existing.get("parent_project_id") if existing else None
        if parent_root is not None:
            parent = self._resolve_root_in_registry(registry, parent_root)
            parent_path = Path(parent["project_root"])
            try:
                relative = Path(root).relative_to(parent_path)
            except ValueError as exc:
                raise MemoryError(
                    "subproject root must be inside its parent project root",
                    code="outside_parent",
                    category="configuration",
                    phase="enroll",
                ) from exc
            if not relative.parts:
                raise MemoryError(
                    "a project cannot be its own parent",
                    code="invalid_parent",
                    category="configuration",
                    phase="enroll",
                )
            parent_project_id = parent["project_id"]
        elif parent_project_id:
            parent = self._entry_by_id(registry, parent_project_id)
            if not parent:
                raise MemoryError(
                    "enrolled project references an unknown parent project",
                    code="parent_not_enrolled",
                    category="configuration",
                    phase="enroll",
                )

        if project_name is None:
            name = existing.get("project_name") if existing else Path(root).name
        elif isinstance(project_name, str) and project_name.strip():
            name = project_name.strip()
        else:
            raise MemoryError("project name must be a non-empty string", code="invalid_project_name")
        name = name or "project"
        default_key_segment = Path(root).name or "project"

        if project_key is not None:
            key = normalize_project_key(project_key)
        elif parent_root is not None and parent:
            key = normalize_project_key(f"{parent['project_key']}/{default_key_segment}")
        elif existing:
            key = existing["project_key"]
        else:
            key = normalize_project_key(default_key_segment)
        if parent:
            parent_prefix = f"{parent['project_key']}/"
            if not key.casefold().startswith(parent_prefix.casefold()):
                raise MemoryError(
                    f"subproject key must be below parent key '{parent['project_key']}'",
                    code="invalid_parent_key",
                    category="configuration",
                    phase="enroll",
                )
        conflicting = self._entry_by_key(registry, key)
        if conflicting and conflicting.get("project_root") != root:
            raise MemoryError(
                f"project key is already enrolled: {key}",
                code="project_key_conflict",
                category="configuration",
                phase="enroll",
            )
        if existing and key.casefold() != existing["project_key"].casefold():
            has_children = any(
                child.get("parent_project_id") == existing["project_id"]
                for child in registry["projects"].values()
            )
            if parent_root is None:
                raise MemoryError(
                    "project key is stable; supply a parent only when attaching an existing standalone project",
                    code="project_key_stable",
                    category="configuration",
                    phase="enroll",
                )
            if has_children:
                raise MemoryError(
                    "a project with subprojects cannot be attached beneath another parent",
                    code="project_has_children",
                    category="configuration",
                    phase="enroll",
                )

        remote = ""
        try:
            remote = subprocess.run(
                ["git", "-C", root, "config", "--get", "remote.origin.url"],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        identity = f"{remote}\n{root}" if remote else root
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "project"
        project_id = existing["project_id"] if existing else f"{slug}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
        entry = {
            "project_id": project_id,
            "project_key": key,
            "project_name": name,
            "project_root": root,
            "git_remote": remote,
            "parent_project_id": parent_project_id,
            "allow_test_secrets": (existing or {}).get("allow_test_secrets", False) if allow_test_secrets is None else bool(allow_test_secrets),
            "enrolled_at": existing.get("enrolled_at", utc_now()) if existing else utc_now(),
        }
        registry["projects"][root] = entry
        return registry, entry

    def preview_enrollment(
        self,
        project_root: str,
        project_name: str | None = None,
        allow_test_secrets: bool | None = None,
        parent_root: str | None = None,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        registry, entry = self._prepare_enrollment(
            project_root, project_name, allow_test_secrets, parent_root, project_key
        )
        return self._describe_entry(registry, entry)

    def enroll(
        self,
        project_root: str,
        project_name: str | None = None,
        allow_test_secrets: bool | None = None,
        parent_root: str | None = None,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        registry, entry = self._prepare_enrollment(
            project_root, project_name, allow_test_secrets, parent_root, project_key
        )
        self._save_registry(registry)
        project_dir = self.data_home / "projects" / entry["project_id"]
        self._secure_dir(project_dir)
        connection = self._connect(entry)
        connection.close()
        return self._describe_entry(registry, entry)

    def resolve_project(self, project_root: str) -> dict[str, Any]:
        return self._resolve_root_in_registry(self._load_registry(), project_root)

    def resolve_project_key(self, project_key: str) -> dict[str, Any]:
        registry = self._load_registry()
        entry = self._entry_by_key(registry, project_key)
        if not entry:
            raise MemoryError(
                f"project key is not enrolled: {normalize_project_key(project_key)}",
                code="project_not_enrolled",
                category="configuration",
                phase="resolve_project",
            )
        return entry

    def resolve_project_args(self, args: dict[str, Any]) -> dict[str, Any]:
        project = args.get("project")
        project_root = args.get("project_root")
        if project is not None:
            entry = self.resolve_project_key(require_text(args, "project", 1000))
            if project_root is not None:
                root_entry = self.resolve_project(require_text(args, "project_root", 4096))
                if root_entry["project_id"] != entry["project_id"]:
                    raise MemoryError(
                        "project and project_root identify different enrolled projects",
                        code="selector_mismatch",
                        category="configuration",
                        phase="resolve_project",
                    )
            return entry
        if project_root is not None:
            return self.resolve_project(require_text(args, "project_root", 4096))
        raise MemoryError(
            "project or project_root is required",
            code="project_missing",
            category="configuration",
            phase="resolve_project",
        )

    def _db_path(self, entry: dict[str, Any]) -> Path:
        return self.data_home / "projects" / entry["project_id"] / "memory.sqlite3"

    def _connect(self, entry: dict[str, Any]) -> sqlite3.Connection:
        db_path = self._db_path(entry)
        self._secure_dir(db_path.parent)
        connection = sqlite3.connect(db_path)
        db_path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                problem TEXT NOT NULL DEFAULT '',
                context TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                secret_blob BLOB,
                fingerprint TEXT,
                repetition_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS records_fingerprint_idx ON records(fingerprint, status);
            CREATE TABLE IF NOT EXISTS revisions (
                record_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                PRIMARY KEY(record_id, revision)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                record_id TEXT,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
                record_id UNINDEXED, title, summary, problem, context, body, tags,
                tokenize='unicode61'
            );
            """
        )
        connection.commit()
        return connection

    def _key(self) -> bytes:
        self._secure_dir(self.config_home)
        key_path = self.config_home / "master.key"
        if not key_path.exists():
            key_path.write_bytes(AESGCM.generate_key(bit_length=256))
        key_path.chmod(0o600)
        key = key_path.read_bytes()
        if len(key) != 32:
            raise MemoryError(
                "invalid project-memory master key",
                code="master_key_invalid",
                category="security",
                phase="decrypt",
            )
        return key

    def _encrypt(self, entry: dict[str, Any], value: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        plaintext = compact_json(value).encode("utf-8")
        encrypted = AESGCM(self._key()).encrypt(nonce, plaintext, entry["project_id"].encode())
        return nonce + encrypted

    def _decrypt(self, entry: dict[str, Any], blob: bytes) -> dict[str, Any]:
        plaintext = AESGCM(self._key()).decrypt(
            blob[:12], blob[12:], entry["project_id"].encode()
        )
        return json.loads(plaintext)

    def _snapshot(self, row: sqlite3.Row) -> str:
        result = dict(row)
        if result.get("secret_blob") is not None:
            result["secret_blob"] = "<encrypted>"
        return compact_json(result)

    def _index(self, connection: sqlite3.Connection, record_id: str, payload: dict[str, Any]) -> None:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        connection.execute("DELETE FROM records_fts WHERE record_id=?", (record_id,))
        if row["status"] != "active":
            return
        tags = " ".join(payload.get("tags", []))
        body_parts = []
        for key in ("action", "final_steps", "verification", "outcome", "endpoint", "paths", "notes", "purpose", "device"):
            value = payload.get(key)
            if isinstance(value, (dict, list)):
                body_parts.append(compact_json(value))
            elif isinstance(value, str):
                body_parts.append(value)
        connection.execute(
            "INSERT INTO records_fts(record_id,title,summary,problem,context,body,tags) VALUES(?,?,?,?,?,?,?)",
            (record_id, row["title"], row["summary"], row["problem"], row["context"], " ".join(body_parts), tags),
        )

    def _save_revision(self, connection: sqlite3.Connection, record_id: str) -> None:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        connection.execute(
            "INSERT INTO revisions(record_id,revision,snapshot_json,changed_at) VALUES(?,?,?,?)",
            (record_id, row["revision"], self._snapshot(row), utc_now()),
        )

    def _audit(self, connection: sqlite3.Connection, action: str, record_id: str | None, detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO audit(action,record_id,detail_json,created_at) VALUES(?,?,?,?)",
            (action, record_id, compact_json(detail), utc_now()),
        )

    def _public_record(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        result = {
            "id": row["id"], "kind": row["kind"], "title": row["title"],
            "summary": row["summary"], "problem": row["problem"], "context": row["context"],
            "repetition_count": row["repetition_count"], "status": row["status"],
            "revision": row["revision"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            **payload,
        }
        if row["secret_blob"] is not None:
            result["has_encrypted_secrets"] = True
        return result

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        registry = self._load_registry()
        described = self._describe_entry(registry, entry)
        connection = self._connect(entry)
        counts = {row["kind"]: row["count"] for row in connection.execute(
            "SELECT kind, count(*) AS count FROM records WHERE status='active' GROUP BY kind"
        )}
        connection.close()
        try:
            usage = {
                "available": True,
                "collection_enabled": self.metrics.enabled,
                **self._decorate_usage(self.metrics.summary([entry["project_id"]], 30)[0]),
            }
        except (OSError, sqlite3.Error, TypeError, ValueError):
            usage = {"available": False}
        return {
            "enrolled": True,
            "project_id": entry["project_id"],
            "project": entry["project_key"],
            "project_name": entry["project_name"],
            "project_root": entry["project_root"],
            "parent_project": described["parent_project"],
            "children": described["children"],
            "is_meta_project": described["is_meta_project"],
            "allow_test_secrets": entry["allow_test_secrets"],
            "counts": counts,
            "usage_30d": usage,
            "storage": str(self._db_path(entry)),
            "inside_project": False,
        }

    def search(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        query = require_text(args, "query", 1000)
        limit = int(args.get("limit", 10))
        if limit < 1 or limit > 50:
            raise MemoryError("limit must be between 1 and 50", code="invalid_argument")
        tokens = re.findall(r"\w+", query, re.UNICODE)[:16]
        if not tokens:
            raise MemoryError("query must contain searchable characters", code="invalid_argument")
        fts_query = " AND ".join(f'"{token}"*' for token in tokens)
        connection = self._connect(entry)
        rows = connection.execute(
            """SELECT r.* FROM records_fts f JOIN records r ON r.id=f.record_id
               WHERE records_fts MATCH ? AND r.status='active'
               ORDER BY bm25(records_fts), r.updated_at DESC LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
        connection.close()
        return {"project": entry["project_key"], "query": query, "results": [self._public_record(row) for row in rows]}

    def get(self, args: dict[str, Any], include_test_secrets: bool = False) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        record_id = require_text(args, "record_id", 100)
        connection = self._connect(entry)
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            connection.close()
            raise MemoryError(
                "record not found",
                code="record_not_found",
                category="record",
                phase="read_record",
            )
        result = self._public_record(row)
        if include_test_secrets:
            if row["kind"] != "test_asset" or row["secret_blob"] is None:
                connection.close()
                raise MemoryError(
                    "record is not a test asset with encrypted secrets",
                    code="test_asset_required",
                    category="security",
                    phase="read_record",
                )
            if not entry.get("allow_test_secrets"):
                connection.close()
                raise MemoryError(
                    "test-secret access is not allowed for this project",
                    code="test_secrets_disabled",
                    category="security",
                    phase="read_record",
                )
            result["secret_fields"] = self._decrypt(entry, row["secret_blob"])
            self._audit(connection, "reveal_test_asset", record_id, {"approved_scope": "test_only"})
            connection.commit()
        connection.close()
        result["project"] = entry["project_key"]
        return result

    def note_repetition(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        problem = require_text(args, "problem")
        action = require_text(args, "action")
        context = str(args.get("context", "")).strip()
        observation = str(args.get("observation", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        ordinary = {"problem": problem, "action": action, "context": context, "observation": observation, "tags": tags}
        found = find_sensitive(ordinary)
        if found:
            raise MemoryError(
                f"credential-like value in {found}; store test credentials only with project_memory_store_test_asset",
                code="sensitive_content_rejected",
                category="security",
                phase="validate_content",
            )
        fingerprint = normalize_fingerprint(problem, action)
        now = utc_now()
        connection = self._connect(entry)
        row = connection.execute(
            "SELECT * FROM records WHERE fingerprint=? AND kind='candidate' AND status='active'",
            (fingerprint,),
        ).fetchone()
        if row:
            payload = json.loads(row["payload_json"])
            observations = payload.setdefault("observations", [])
            if observation:
                observations.append({"at": now, "text": observation})
                del observations[:-50]
            payload["tags"] = sorted(set(payload.get("tags", []) + tags))
            revision = row["revision"] + 1
            connection.execute(
                "UPDATE records SET context=?,payload_json=?,repetition_count=repetition_count+1,revision=?,updated_at=? WHERE id=?",
                (context or row["context"], compact_json(payload), revision, now, row["id"]),
            )
            record_id = row["id"]
            action_name = "repeat_candidate"
        else:
            record_id = str(uuid.uuid4())
            payload = {"action": action, "observations": ([{"at": now, "text": observation}] if observation else []), "tags": tags}
            connection.execute(
                """INSERT INTO records(id,kind,title,summary,problem,context,payload_json,fingerprint,repetition_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, "candidate", f"Candidate: {action[:120]}", observation, problem, context, compact_json(payload), fingerprint, 1, now, now),
            )
            action_name = "create_candidate"
        self._save_revision(connection, record_id)
        payload = json.loads(connection.execute("SELECT payload_json FROM records WHERE id=?", (record_id,)).fetchone()[0])
        self._index(connection, record_id, payload)
        self._audit(connection, action_name, record_id, {"fingerprint": fingerprint})
        connection.commit()
        result = self._public_record(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
        connection.close()
        result["project"] = entry["project_key"]
        result["eligible_to_finalize"] = result["repetition_count"] >= 2
        return result

    def finalize_solution(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        record_id = require_text(args, "candidate_id", 100)
        title = require_text(args, "title", 500)
        final_steps = string_list(args.get("final_steps"), "final_steps", 100)
        if not final_steps:
            raise MemoryError("final_steps must not be empty", code="invalid_argument")
        verification = require_text(args, "verification")
        outcome = require_text(args, "outcome")
        context = str(args.get("context", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        ordinary = {"title": title, "final_steps": final_steps, "verification": verification, "outcome": outcome, "context": context, "tags": tags}
        found = find_sensitive(ordinary)
        if found:
            raise MemoryError(
                f"credential-like value in {found}; keep credentials in a test asset and reference its id",
                code="sensitive_content_rejected",
                category="security",
                phase="validate_content",
            )
        connection = self._connect(entry)
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row or row["kind"] != "candidate" or row["status"] != "active":
            connection.close()
            raise MemoryError(
                "active candidate not found",
                code="candidate_not_found",
                category="record",
                phase="finalize",
            )
        if row["repetition_count"] < 2:
            connection.close()
            raise MemoryError(
                "candidate needs at least two recorded occurrences before finalization",
                code="candidate_not_ready",
                category="workflow",
                phase="finalize",
            )
        old_payload = json.loads(row["payload_json"])
        payload = {
            "final_steps": final_steps,
            "verification": verification,
            "outcome": outcome,
            "attempt_history": old_payload.get("observations", []),
            "tags": sorted(set(old_payload.get("tags", []) + tags)),
        }
        revision = row["revision"] + 1
        now = utc_now()
        connection.execute(
            """UPDATE records SET kind='solution',title=?,summary=?,context=?,payload_json=?,revision=?,updated_at=? WHERE id=?""",
            (title, outcome, context or row["context"], compact_json(payload), revision, now, record_id),
        )
        self._save_revision(connection, record_id)
        self._index(connection, record_id, payload)
        self._audit(connection, "finalize_solution", record_id, {"verified": True, "repetitions": row["repetition_count"]})
        connection.commit()
        result = self._public_record(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
        connection.close()
        result["project"] = entry["project_key"]
        return result

    def store_test_asset(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        if args.get("test_only") is not True:
            raise MemoryError("test_only must be explicitly true", code="test_only_required")
        if not entry.get("allow_test_secrets"):
            raise MemoryError(
                "this project is not enrolled for test-equipment secrets",
                code="test_secrets_disabled",
                category="security",
                phase="store_test_asset",
            )
        name = require_text(args, "name", 500)
        asset_type = require_text(args, "asset_type", 200)
        endpoint = str(args.get("endpoint", "")).strip()
        username = str(args.get("username", "")).strip()
        paths = args.get("paths", {})
        notes = str(args.get("notes", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        secret_fields = args.get("secret_fields", {})
        if not isinstance(paths, dict) or not isinstance(secret_fields, dict):
            raise MemoryError("paths and secret_fields must be objects", code="invalid_argument")
        if not secret_fields:
            raise MemoryError(
                "secret_fields must contain at least one test credential",
                code="invalid_argument",
            )
        public_payload = {"asset_type": asset_type, "endpoint": endpoint, "username": username, "paths": paths, "notes": notes, "tags": tags, "test_only": True}
        found = find_sensitive(public_payload)
        if found:
            raise MemoryError(
                f"credential-like value in public field {found}; move it to secret_fields",
                code="sensitive_content_rejected",
                category="security",
                phase="validate_content",
            )
        record_id = str(uuid.uuid4())
        now = utc_now()
        connection = self._connect(entry)
        connection.execute(
            """INSERT INTO records(id,kind,title,summary,context,payload_json,secret_blob,fingerprint,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (record_id, "test_asset", name, f"Test-only {asset_type}", notes, compact_json(public_payload), self._encrypt(entry, secret_fields), normalize_fingerprint("test_asset", name, endpoint), now, now),
        )
        self._save_revision(connection, record_id)
        self._index(connection, record_id, public_payload)
        self._audit(connection, "store_test_asset", record_id, {"test_only": True, "secret_field_names": sorted(secret_fields)})
        connection.commit()
        result = self._public_record(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
        connection.close()
        result["project"] = entry["project_key"]
        return result

    def record_log_location(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        name = require_text(args, "name", 500)
        path = require_text(args, "path", 4096)
        purpose = require_text(args, "purpose")
        device = str(args.get("device", "")).strip()
        notes = str(args.get("notes", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        payload = {"path": path, "purpose": purpose, "device": device, "notes": notes, "tags": tags}
        found = find_sensitive(payload)
        if found:
            raise MemoryError(
                f"credential-like value in {found}; log locations must not embed credentials",
                code="sensitive_content_rejected",
                category="security",
                phase="validate_content",
            )
        record_id = str(uuid.uuid4())
        now = utc_now()
        connection = self._connect(entry)
        connection.execute(
            """INSERT INTO records(id,kind,title,summary,context,payload_json,fingerprint,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (record_id, "log_location", name, purpose, device, compact_json(payload), normalize_fingerprint("log", name, path), now, now),
        )
        self._save_revision(connection, record_id)
        self._index(connection, record_id, payload)
        self._audit(connection, "record_log_location", record_id, {"path": path})
        connection.commit()
        result = self._public_record(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
        connection.close()
        result["project"] = entry["project_key"]
        return result

    def deprecate(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        record_id = require_text(args, "record_id", 100)
        reason = require_text(args, "reason")
        if find_sensitive(reason):
            raise MemoryError(
                "deprecation reason must not contain a credential",
                code="sensitive_content_rejected",
                category="security",
                phase="validate_content",
            )
        connection = self._connect(entry)
        row = connection.execute("SELECT * FROM records WHERE id=? AND status='active'", (record_id,)).fetchone()
        if not row:
            connection.close()
            raise MemoryError(
                "active record not found",
                code="record_not_found",
                category="record",
                phase="deprecate",
            )
        payload = json.loads(row["payload_json"])
        payload["deprecated_reason"] = reason
        revision = row["revision"] + 1
        connection.execute(
            "UPDATE records SET status='deprecated',payload_json=?,revision=?,updated_at=? WHERE id=?",
            (compact_json(payload), revision, utc_now(), record_id),
        )
        self._save_revision(connection, record_id)
        self._index(connection, record_id, payload)
        self._audit(connection, "deprecate", record_id, {"reason": reason})
        connection.commit()
        result = self._public_record(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
        connection.close()
        result["project"] = entry["project_key"]
        return result

    def mark_used(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        record_id = require_text(args, "record_id", 100)
        outcome = require_text(args, "outcome", 100).casefold()
        allowed = {"reused", "helpful", "not_applicable", "stale"}
        if outcome not in allowed:
            raise MemoryError(
                "outcome must be reused, helpful, not_applicable, or stale",
                code="invalid_argument",
                phase="mark_used",
            )
        connection = self._connect(entry)
        row = connection.execute("SELECT id,kind,status FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            connection.close()
            raise MemoryError(
                "record not found",
                code="record_not_found",
                category="record",
                phase="mark_used",
            )
        self._audit(connection, "mark_used", record_id, {"outcome": outcome})
        connection.commit()
        connection.close()
        return {
            "project": entry["project_key"],
            "record_id": record_id,
            "record_kind": row["kind"],
            "record_status": row["status"],
            "outcome": outcome,
        }

    def _metric_entries(
        self,
        *,
        project: str | None = None,
        project_root: str | None = None,
        all_projects: bool = False,
        include_children: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        registry = self._load_registry()
        if all_projects:
            entries = list(registry["projects"].values())
        else:
            selector: dict[str, Any] = {}
            if project is not None:
                selector["project"] = project
            if project_root is not None:
                selector["project_root"] = project_root
            selected = self.resolve_project_args(selector)
            entries = [selected]
            if include_children:
                known_ids = {selected["project_id"]}
                changed = True
                while changed:
                    changed = False
                    for candidate in registry["projects"].values():
                        if (
                            candidate.get("parent_project_id") in known_ids
                            and candidate["project_id"] not in known_ids
                        ):
                            known_ids.add(candidate["project_id"])
                            entries.append(candidate)
                            changed = True
        entries.sort(key=lambda value: value["project_key"].casefold())
        return registry, entries

    @staticmethod
    def _decorate_usage(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        searches = int(result.get("searches", 0))
        result["search_hit_rate"] = round(
            (int(result.get("search_hits", 0)) / searches * 100.0) if searches else 0.0,
            1,
        )
        result.pop("project_id", None)
        return result

    def metrics_report(
        self,
        *,
        project: str | None = None,
        project_root: str | None = None,
        all_projects: bool = False,
        include_children: bool = False,
        since_days: int = 30,
        error_limit: int = 10,
    ) -> dict[str, Any]:
        registry, entries = self._metric_entries(
            project=project,
            project_root=project_root,
            all_projects=all_projects,
            include_children=include_children,
        )
        identifiers = [entry["project_id"] for entry in entries]
        rows = {
            row["project_id"]: row for row in self.metrics.summary(identifiers, since_days)
        }
        projects = []
        for entry in entries:
            described = self._describe_entry(registry, entry)
            projects.append(
                {
                    "project": entry["project_key"],
                    "project_name": entry["project_name"],
                    "parent_project": described["parent_project"],
                    "is_meta_project": described["is_meta_project"],
                    **self._decorate_usage(rows[entry["project_id"]]),
                }
            )
        errors = self.metrics.error_summary(
            identifiers,
            since_days,
            limit=error_limit,
        )
        keys_by_id = {entry["project_id"]: entry["project_key"] for entry in entries}
        for error in errors:
            error["project"] = keys_by_id.get(error.pop("project_id"), "<unresolved>")
        return {
            "since_days": since_days,
            "metrics_enabled": self.metrics.enabled,
            "projects": projects,
            "top_errors": errors,
        }

    def usage_stats(self, args: dict[str, Any]) -> dict[str, Any]:
        since_days = int(args.get("since_days", 30))
        include_children = bool(args.get("include_children", False))
        error_limit = int(args.get("error_limit", 10))
        if error_limit < 0 or error_limit > 50:
            raise MemoryError("error_limit must be between 0 and 50", code="invalid_argument")
        return self.metrics_report(
            project=args.get("project"),
            project_root=args.get("project_root"),
            include_children=include_children,
            since_days=since_days,
            error_limit=error_limit,
        )

    def backup(self, project_root: str | None = None, project: str | None = None) -> Path:
        selector: dict[str, Any] = {}
        if project is not None:
            selector["project"] = project
        if project_root is not None:
            selector["project_root"] = project_root
        entry = self.resolve_project_args(selector)
        source = self._db_path(entry)
        backup_dir = source.parent / "backups"
        self._secure_dir(backup_dir)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = backup_dir / f"memory-{stamp}.sqlite3"
        source_connection = self._connect(entry)
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection)
        destination_connection.close()
        source_connection.close()
        destination.chmod(0o600)
        return destination


PROJECT_SELECTOR_PROPERTIES: dict[str, Any] = {
    "project": {
        "type": "string",
        "description": "Hierarchical project key from active workspace instructions; prefer a local AGENTS.override.md for private mappings, for example ExampleSuite/c-port.",
    },
    "project_root": {
        "type": "string",
        "description": "Legacy exact enrolled project root. Prefer project for new configurations.",
    },
}


def tool_input(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**PROJECT_SELECTOR_PROPERTIES, **properties},
        "required": required,
        "anyOf": [{"required": ["project"]}, {"required": ["project_root"]}],
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "project_memory_status",
        "title": "Project Memory Status",
        "description": "Confirm enrollment and show record counts, 30-day usage, and parent/child routing for one project key.",
        "inputSchema": tool_input({}, []),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "project_memory_search",
        "title": "Search Project Memory",
        "description": "Search one selected project's non-secret memory. Search its parent separately when active workspace guidance directs it.",
        "inputSchema": tool_input(
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            ["query"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "project_memory_get",
        "title": "Get Project Memory Record",
        "description": "Read an ordinary record or test-asset metadata from one selected project without revealing encrypted credentials.",
        "inputSchema": tool_input({"record_id": {"type": "string"}}, ["record_id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "project_memory_get_test_asset",
        "title": "Reveal Test Asset Credentials",
        "description": "Decrypt credentials for an explicitly test-only asset in one selected project. Never reproduce secrets elsewhere.",
        "inputSchema": tool_input({"record_id": {"type": "string"}}, ["record_id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "project_memory_note_repetition",
        "title": "Note Repeated Action",
        "description": "Track another occurrence in one selected project while a final variant is still being sought.",
        "inputSchema": tool_input(
            {
                "problem": {"type": "string"},
                "action": {"type": "string"},
                "context": {"type": "string"},
                "observation": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            ["problem", "action"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "project_memory_finalize_solution",
        "title": "Finalize Verified Solution",
        "description": "Convert a candidate in one selected project into a verified solution after two occurrences.",
        "inputSchema": tool_input(
            {
                "candidate_id": {"type": "string"},
                "title": {"type": "string"},
                "final_steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "verification": {"type": "string"},
                "outcome": {"type": "string"},
                "context": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            ["candidate_id", "title", "final_steps", "verification", "outcome"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "project_memory_store_test_asset",
        "title": "Store Test Equipment",
        "description": "Store test-only routing and encrypted credentials in one selected project that allows test secrets.",
        "inputSchema": tool_input(
            {
                "test_only": {"type": "boolean", "const": True},
                "name": {"type": "string"},
                "asset_type": {"type": "string"},
                "endpoint": {"type": "string"},
                "username": {"type": "string"},
                "secret_fields": {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "number", "boolean"]},
                },
                "paths": {"type": "object", "additionalProperties": {"type": "string"}},
                "notes": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            ["test_only", "name", "asset_type", "secret_fields"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "project_memory_record_log_location",
        "title": "Record Log Location",
        "description": "Record a stable log location in one selected project without copying raw logs or credentials.",
        "inputSchema": tool_input(
            {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "purpose": {"type": "string"},
                "device": {"type": "string"},
                "notes": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            ["name", "path", "purpose"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "project_memory_mark_used",
        "title": "Mark Memory Outcome",
        "description": "Record whether a retrieved memory was reused, helpful, not applicable, or stale.",
        "inputSchema": tool_input(
            {
                "record_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["reused", "helpful", "not_applicable", "stale"],
                },
            },
            ["record_id", "outcome"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "project_memory_stats",
        "title": "Project Memory Usage Statistics",
        "description": "Read local aggregate usage and sanitized error statistics for one project and optionally its children.",
        "inputSchema": tool_input(
            {
                "since_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 36500,
                    "default": 30,
                },
                "include_children": {"type": "boolean", "default": False},
                "error_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 50,
                    "default": 10,
                },
            },
            [],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "project_memory_deprecate",
        "title": "Deprecate Project Memory Record",
        "description": "Soft-deprecate an obsolete record in one selected project while preserving history.",
        "inputSchema": tool_input(
            {"record_id": {"type": "string"}, "reason": {"type": "string"}},
            ["record_id", "reason"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


TOOL_OPERATIONS = {
    "project_memory_status": "probe",
    "project_memory_search": "read",
    "project_memory_get": "read",
    "project_memory_get_test_asset": "sensitive_read",
    "project_memory_note_repetition": "write",
    "project_memory_finalize_solution": "edit",
    "project_memory_store_test_asset": "create",
    "project_memory_record_log_location": "create",
    "project_memory_mark_used": "feedback",
    "project_memory_stats": "report",
    "project_memory_deprecate": "edit",
}


def project_id_hint(memory: ProjectMemory, args: dict[str, Any]) -> str | None:
    if not isinstance(args, dict):
        return None
    try:
        registry = memory._load_registry()
        project = args.get("project")
        if isinstance(project, str) and project.strip():
            entry = memory._entry_by_key(registry, project)
            return entry["project_id"] if entry else None
        project_root = args.get("project_root")
        if isinstance(project_root, str) and project_root.strip():
            entry = registry["projects"].get(canonical(project_root))
            return entry["project_id"] if entry else None
    except (MemoryError, OSError, RuntimeError):
        pass
    return None


def successful_operation(name: str, result: dict[str, Any]) -> str:
    if name == "project_memory_note_repetition":
        return "create" if int(result.get("repetition_count", 0)) == 1 else "edit"
    if name == "project_memory_mark_used":
        outcome = str(result.get("outcome", ""))
        return "reuse" if outcome == "reused" else outcome
    return TOOL_OPERATIONS.get(name, "tool_call")


def public_error_message(exc: Exception) -> str:
    if isinstance(exc, MemoryError):
        return str(exc)
    if isinstance(exc, InvalidTag):
        return "encrypted test-asset authentication failed"
    if isinstance(exc, sqlite3.Error):
        return "Project Memory storage operation failed"
    if isinstance(exc, OSError):
        return "Project Memory filesystem operation failed"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid Project Memory request"
    return f"internal project-memory error: {type(exc).__name__}"


def record_metric_safely(memory: ProjectMemory, **values: Any) -> str | None:
    try:
        return memory.metrics.record_call(**values)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def call_tool(memory: ProjectMemory, name: str, args: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "project_memory_status": memory.status,
        "project_memory_search": memory.search,
        "project_memory_get": memory.get,
        "project_memory_get_test_asset": lambda value: memory.get(value, include_test_secrets=True),
        "project_memory_note_repetition": memory.note_repetition,
        "project_memory_finalize_solution": memory.finalize_solution,
        "project_memory_store_test_asset": memory.store_test_asset,
        "project_memory_record_log_location": memory.record_log_location,
        "project_memory_mark_used": memory.mark_used,
        "project_memory_stats": memory.usage_stats,
        "project_memory_deprecate": memory.deprecate,
    }
    started = time.monotonic_ns()
    project_id = project_id_hint(memory, args)
    metric_tool = name if name in handlers else "<unknown>"
    try:
        if not isinstance(args, dict):
            raise MemoryError("tool arguments must be an object", code="invalid_argument")
        if name not in handlers:
            raise MemoryError(
                f"unknown tool: {name}",
                code="unknown_tool",
                category="protocol",
                phase="dispatch",
            )
        result = handlers[name](args)
        if name != "project_memory_stats":
            result_items = len(result.get("results", [])) if name == "project_memory_search" else 0
            record_metric_safely(
                memory,
                project_id=project_id,
                tool=metric_tool,
                operation=successful_operation(name, result),
                success=True,
                duration_us=(time.monotonic_ns() - started) // 1000,
                result_items=result_items,
            )
        return result
    except Exception as exc:
        detail = describe_exception(exc)
        error_id = record_metric_safely(
            memory,
            project_id=project_id,
            tool=metric_tool,
            operation=TOOL_OPERATIONS.get(name, "tool_call"),
            success=False,
            duration_us=(time.monotonic_ns() - started) // 1000,
            error=detail,
        )
        raise ReportedToolError(public_error_message(exc), detail, error_id) from exc


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reported_error_result(exc: ReportedToolError) -> dict[str, Any]:
    error = {
        "message": str(exc),
        "code": exc.detail["error_code"],
        "category": exc.detail["category"],
        "phase": exc.detail["phase"],
        "exception_type": exc.detail["exception_type"],
    }
    if exc.detail.get("system_code"):
        error["system_code"] = exc.detail["system_code"]
    if exc.error_id:
        error["error_id"] = exc.error_id
    identifier = f" [error_id: {exc.error_id}]" if exc.error_id else ""
    return {
        "content": [{"type": "text", "text": f"{exc}{identifier}"}],
        "structuredContent": {"error": error},
        "isError": True,
    }


def serve() -> None:
    memory = ProjectMemory()
    for line in sys.stdin:
        request_id = None
        try:
            message = json.loads(line)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "instructions": "Use the hierarchical project key mapped by active workspace instructions. Prefer a Git-excluded local AGENTS.override.md for machine-specific mappings. Search a subproject and its declared parent separately, write to the exact selected project, finalize only verified solutions, mark whether retrieved records were useful, and use local sanitized statistics only when requested."}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = message.get("params", {})
                result = call_tool(memory, params.get("name", ""), params.get("arguments", {}))
                send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}], "structuredContent": result, "isError": False}})
            elif request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}})
        except ReportedToolError as exc:
            send({"jsonrpc": "2.0", "id": request_id, "result": reported_error_result(exc)})
        except Exception as exc:  # Keep protocol alive; do not expose raw exception details.
            detail = describe_exception(exc)
            error_id = record_metric_safely(
                memory,
                project_id=None,
                tool="<protocol>",
                operation="protocol",
                success=False,
                duration_us=0,
                error=detail,
            )
            reported = ReportedToolError(public_error_message(exc), detail, error_id)
            if isinstance(exc, json.JSONDecodeError):
                send({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "parse error",
                        "data": reported_error_result(reported)["structuredContent"],
                    },
                })
            elif request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "result": reported_error_result(reported)})
    memory.metrics.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    enroll_parser = subparsers.add_parser("enroll")
    enroll_parser.add_argument("--project-root", help="Project root, or parent root when --subproject is used; defaults to the current directory")
    enroll_parser.add_argument("--project-name", help="Display name; defaults to the project directory name")
    enroll_parser.add_argument("--project-key", help="Stable hierarchical selector; defaults to the folder name under its parent key")
    enroll_parser.add_argument("--parent-root", help="Existing enrolled parent for the project root")
    enroll_parser.add_argument("--subproject", help="Child path relative to the parent project root")
    enroll_parser.add_argument("--allow-test-secrets", action="store_true", default=None)
    enroll_parser.add_argument("-y", "--yes", action="store_true", help="Skip interactive confirmation")
    backup_parser = subparsers.add_parser("backup")
    backup_selector = backup_parser.add_mutually_exclusive_group(required=True)
    backup_selector.add_argument("--project-root")
    backup_selector.add_argument("--project")
    args = parser.parse_args()
    memory = ProjectMemory()
    if args.command == "serve":
        serve()
    elif args.command == "enroll":
        if args.subproject and args.parent_root:
            parser.error("--subproject and --parent-root cannot be used together")
        if args.subproject:
            parent_root = args.project_root or os.getcwd()
            child = Path(args.subproject).expanduser()
            project_root = str(child if child.is_absolute() else Path(parent_root) / child)
        else:
            project_root = args.project_root or os.getcwd()
            parent_root = args.parent_root
        preview = memory.preview_enrollment(
            project_root,
            args.project_name,
            args.allow_test_secrets,
            parent_root,
            args.project_key,
        )
        if sys.stdin.isatty() and not args.yes:
            print("Enroll Project Memory?", file=sys.stderr)
            print(f"  Root:    {preview['project_root']}", file=sys.stderr)
            print(f"  Name:    {preview['project_name']}", file=sys.stderr)
            print(f"  Project: {preview['project_key']}", file=sys.stderr)
            if preview["parent_project"]:
                print(f"  Parent:  {preview['parent_project']}", file=sys.stderr)
            print("Continue? [Y/n] ", end="", file=sys.stderr, flush=True)
            answer = sys.stdin.readline().strip().casefold()
            if answer not in {"", "y", "yes"}:
                print("Enrollment cancelled.", file=sys.stderr)
                return
        result = memory.enroll(
            project_root,
            args.project_name,
            args.allow_test_secrets,
            parent_root,
            args.project_key,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "backup":
        print(memory.backup(args.project_root, args.project))


if __name__ == "__main__":
    main()
