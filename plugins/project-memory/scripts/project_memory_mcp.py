#!/usr/bin/env python3
"""Project-scoped operational memory MCP server (stdlib + cryptography)."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SERVER_NAME = "Project Memory"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-11-25"
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:password|passwd|pwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+", re.I),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I),
)


class MemoryError(Exception):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def canonical(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=True))


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
        raise MemoryError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise MemoryError(f"{name} exceeds {limit} characters")
    return value


def string_list(value: Any, name: str, limit: int = 50) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise MemoryError(f"{name} must be a list with at most {limit} items")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MemoryError(f"{name} items must be non-empty strings")
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

    def _secure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"schema_version": 1, "projects": {}}
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryError(f"cannot read registry: {exc}") from exc

    def _save_registry(self, registry: dict[str, Any]) -> None:
        self._secure_dir(self.data_home)
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.registry_path)
        self.registry_path.chmod(0o600)

    def enroll(self, project_root: str, project_name: str, allow_test_secrets: bool) -> dict[str, Any]:
        root = canonical(project_root)
        if not Path(root).is_dir():
            raise MemoryError("project root must be a directory")
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
        slug = re.sub(r"[^a-z0-9]+", "-", project_name.casefold()).strip("-") or "project"
        project_id = f"{slug}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
        registry = self._load_registry()
        existing = registry["projects"].get(root)
        if existing and existing["project_id"] != project_id:
            raise MemoryError("project root is already enrolled under a different identity")
        entry = {
            "project_id": project_id,
            "project_name": project_name,
            "project_root": root,
            "git_remote": remote,
            "allow_test_secrets": bool(allow_test_secrets),
            "enrolled_at": existing.get("enrolled_at", utc_now()) if existing else utc_now(),
        }
        registry["projects"][root] = entry
        self._save_registry(registry)
        project_dir = self.data_home / "projects" / project_id
        self._secure_dir(project_dir)
        connection = self._connect(entry)
        connection.close()
        return entry

    def resolve_project(self, project_root: str) -> dict[str, Any]:
        try:
            root = canonical(project_root)
        except (OSError, RuntimeError) as exc:
            raise MemoryError(f"invalid project root: {exc}") from exc
        entry = self._load_registry().get("projects", {}).get(root)
        if not entry:
            raise MemoryError(f"project is not enrolled: {root}")
        if entry.get("project_root") != root:
            raise MemoryError("registry project-root mismatch")
        return entry

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
            raise MemoryError("invalid project-memory master key")
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
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        connection = self._connect(entry)
        counts = {row["kind"]: row["count"] for row in connection.execute(
            "SELECT kind, count(*) AS count FROM records WHERE status='active' GROUP BY kind"
        )}
        connection.close()
        return {
            "enrolled": True,
            "project_id": entry["project_id"],
            "project_name": entry["project_name"],
            "allow_test_secrets": entry["allow_test_secrets"],
            "counts": counts,
            "storage": str(self._db_path(entry)),
            "inside_project": False,
        }

    def search(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        query = require_text(args, "query", 1000)
        limit = int(args.get("limit", 10))
        if limit < 1 or limit > 50:
            raise MemoryError("limit must be between 1 and 50")
        tokens = re.findall(r"\w+", query, re.UNICODE)[:16]
        if not tokens:
            raise MemoryError("query must contain searchable characters")
        fts_query = " AND ".join(f'"{token}"*' for token in tokens)
        connection = self._connect(entry)
        rows = connection.execute(
            """SELECT r.* FROM records_fts f JOIN records r ON r.id=f.record_id
               WHERE records_fts MATCH ? AND r.status='active'
               ORDER BY bm25(records_fts), r.updated_at DESC LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
        connection.close()
        return {"query": query, "results": [self._public_record(row) for row in rows]}

    def get(self, args: dict[str, Any], include_test_secrets: bool = False) -> dict[str, Any]:
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        record_id = require_text(args, "record_id", 100)
        connection = self._connect(entry)
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            connection.close()
            raise MemoryError("record not found")
        result = self._public_record(row)
        if include_test_secrets:
            if row["kind"] != "test_asset" or row["secret_blob"] is None:
                connection.close()
                raise MemoryError("record is not a test asset with encrypted secrets")
            if not entry.get("allow_test_secrets"):
                connection.close()
                raise MemoryError("test-secret access is not allowed for this project")
            result["secret_fields"] = self._decrypt(entry, row["secret_blob"])
            self._audit(connection, "reveal_test_asset", record_id, {"approved_scope": "test_only"})
            connection.commit()
        connection.close()
        return result

    def note_repetition(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        problem = require_text(args, "problem")
        action = require_text(args, "action")
        context = str(args.get("context", "")).strip()
        observation = str(args.get("observation", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        ordinary = {"problem": problem, "action": action, "context": context, "observation": observation, "tags": tags}
        found = find_sensitive(ordinary)
        if found:
            raise MemoryError(f"credential-like value in {found}; store test credentials only with project_memory_store_test_asset")
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
        result["eligible_to_finalize"] = result["repetition_count"] >= 2
        return result

    def finalize_solution(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        record_id = require_text(args, "candidate_id", 100)
        title = require_text(args, "title", 500)
        final_steps = string_list(args.get("final_steps"), "final_steps", 100)
        if not final_steps:
            raise MemoryError("final_steps must not be empty")
        verification = require_text(args, "verification")
        outcome = require_text(args, "outcome")
        context = str(args.get("context", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        ordinary = {"title": title, "final_steps": final_steps, "verification": verification, "outcome": outcome, "context": context, "tags": tags}
        found = find_sensitive(ordinary)
        if found:
            raise MemoryError(f"credential-like value in {found}; keep credentials in a test asset and reference its id")
        connection = self._connect(entry)
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row or row["kind"] != "candidate" or row["status"] != "active":
            connection.close()
            raise MemoryError("active candidate not found")
        if row["repetition_count"] < 2:
            connection.close()
            raise MemoryError("candidate needs at least two recorded occurrences before finalization")
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
        return result

    def store_test_asset(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        if args.get("test_only") is not True:
            raise MemoryError("test_only must be explicitly true")
        if not entry.get("allow_test_secrets"):
            raise MemoryError("this project is not enrolled for test-equipment secrets")
        name = require_text(args, "name", 500)
        asset_type = require_text(args, "asset_type", 200)
        endpoint = str(args.get("endpoint", "")).strip()
        username = str(args.get("username", "")).strip()
        paths = args.get("paths", {})
        notes = str(args.get("notes", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        secret_fields = args.get("secret_fields", {})
        if not isinstance(paths, dict) or not isinstance(secret_fields, dict):
            raise MemoryError("paths and secret_fields must be objects")
        if not secret_fields:
            raise MemoryError("secret_fields must contain at least one test credential")
        public_payload = {"asset_type": asset_type, "endpoint": endpoint, "username": username, "paths": paths, "notes": notes, "tags": tags, "test_only": True}
        found = find_sensitive(public_payload)
        if found:
            raise MemoryError(f"credential-like value in public field {found}; move it to secret_fields")
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
        return result

    def record_log_location(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        name = require_text(args, "name", 500)
        path = require_text(args, "path", 4096)
        purpose = require_text(args, "purpose")
        device = str(args.get("device", "")).strip()
        notes = str(args.get("notes", "")).strip()
        tags = string_list(args.get("tags"), "tags")
        payload = {"path": path, "purpose": purpose, "device": device, "notes": notes, "tags": tags}
        found = find_sensitive(payload)
        if found:
            raise MemoryError(f"credential-like value in {found}; log locations must not embed credentials")
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
        return result

    def deprecate(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project(require_text(args, "project_root", 4096))
        record_id = require_text(args, "record_id", 100)
        reason = require_text(args, "reason")
        if find_sensitive(reason):
            raise MemoryError("deprecation reason must not contain a credential")
        connection = self._connect(entry)
        row = connection.execute("SELECT * FROM records WHERE id=? AND status='active'", (record_id,)).fetchone()
        if not row:
            connection.close()
            raise MemoryError("active record not found")
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
        return result

    def backup(self, project_root: str) -> Path:
        entry = self.resolve_project(project_root)
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


TOOLS: list[dict[str, Any]] = [
    {"name": "project_memory_status", "title": "Project Memory Status", "description": "Confirm that the exact project root is enrolled and show local record counts.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}}, "required": ["project_root"]}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "project_memory_search", "title": "Search Project Memory", "description": "Search non-secret project memory before repeating troubleshooting or operational work.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}, "required": ["project_root", "query"]}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "project_memory_get", "title": "Get Project Memory Record", "description": "Read an ordinary record or test-asset metadata without revealing encrypted credentials.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "record_id": {"type": "string"}}, "required": ["project_root", "record_id"]}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "project_memory_get_test_asset", "title": "Reveal Test Asset Credentials", "description": "Decrypt credentials for an explicitly test-only asset in an enrolled project. Use only when required by the current task and never reproduce secrets elsewhere.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "record_id": {"type": "string"}}, "required": ["project_root", "record_id"]}, "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "project_memory_note_repetition", "title": "Note Repeated Action", "description": "Track another occurrence of a recurring problem/action while a final variant is being sought. Credential-like values are rejected.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "problem": {"type": "string"}, "action": {"type": "string"}, "context": {"type": "string"}, "observation": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["project_root", "problem", "action"]}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "project_memory_finalize_solution", "title": "Finalize Verified Solution", "description": "Convert a candidate with at least two occurrences into a solution after real verification succeeded.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "candidate_id": {"type": "string"}, "title": {"type": "string"}, "final_steps": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "verification": {"type": "string"}, "outcome": {"type": "string"}, "context": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["project_root", "candidate_id", "title", "final_steps", "verification", "outcome"]}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "project_memory_store_test_asset", "title": "Store Test Equipment", "description": "Store test-only equipment routing, paths, username, and encrypted credentials. Requires an enrolled project that explicitly allows test secrets.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "test_only": {"type": "boolean", "const": True}, "name": {"type": "string"}, "asset_type": {"type": "string"}, "endpoint": {"type": "string"}, "username": {"type": "string"}, "secret_fields": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}, "paths": {"type": "object", "additionalProperties": {"type": "string"}}, "notes": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["project_root", "test_only", "name", "asset_type", "secret_fields"]}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "project_memory_record_log_location", "title": "Record Log Location", "description": "Record a stable local or test-device log path without copying raw logs or credentials.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "name": {"type": "string"}, "path": {"type": "string"}, "purpose": {"type": "string"}, "device": {"type": "string"}, "notes": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["project_root", "name", "path", "purpose"]}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "project_memory_deprecate", "title": "Deprecate Project Memory Record", "description": "Soft-deprecate an obsolete record while preserving revisions and audit history.", "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "record_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["project_root", "record_id", "reason"]}, "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]


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
        "project_memory_deprecate": memory.deprecate,
    }
    if name not in handlers:
        raise MemoryError(f"unknown tool: {name}")
    return handlers[name](args)


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve() -> None:
    memory = ProjectMemory()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "instructions": "Use only with the exact current enrolled project root. Search before repeated work, track recurring attempts, finalize only verified solutions, and keep test credentials inside dedicated encrypted test-asset records."}})
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
        except (MemoryError, ValueError, TypeError, sqlite3.Error) as exc:
            if "request_id" in locals() and request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}})
        except Exception as exc:  # Keep protocol alive; do not expose a traceback to the client.
            if "request_id" in locals() and request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": f"internal project-memory error: {type(exc).__name__}"}], "isError": True}})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    enroll_parser = subparsers.add_parser("enroll")
    enroll_parser.add_argument("--project-root", required=True)
    enroll_parser.add_argument("--project-name", required=True)
    enroll_parser.add_argument("--allow-test-secrets", action="store_true")
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--project-root", required=True)
    args = parser.parse_args()
    memory = ProjectMemory()
    if args.command == "serve":
        serve()
    elif args.command == "enroll":
        print(json.dumps(memory.enroll(args.project_root, args.project_name, args.allow_test_secrets), ensure_ascii=False, indent=2))
    elif args.command == "backup":
        print(memory.backup(args.project_root))


if __name__ == "__main__":
    main()
