#!/usr/bin/env python3
"""Project-scoped operational memory MCP server (stdlib + cryptography)."""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from project_memory_metrics import MetricsStore
from project_memory_views import (
    CONFIDENCE_VALUES,
    IMPORTANCE_VALUES,
    RECORD_KINDS,
    STALE_STATES,
    build_record_views,
    is_complete,
    serialized_bytes,
)


SERVER_NAME = "Project Memory"
SERVER_VERSION = "0.5.1"
PROTOCOL_VERSION = "2025-11-25"
REGISTRY_SCHEMA_VERSION = 3
MEMORY_SCHEMA_VERSION = 2
DEFAULT_TARGET_TOKENS = 3000
DEFAULT_MAX_TOKENS = 8000
DEFAULT_CARDS = 4
MAXIMUM_CARDS = 8
TOKEN_BYTE_RATIO = 3
PROFILE_VALUES = {"lean", "compat", "admin"}
DEFAULT_LONGRUN_SECRET_TTL_SEC = 300
DEFAULT_LONGRUN_MAX_STDIN_SECRET_BYTES = 64 * 1024
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
    def __init__(
        self,
        data_home: Path | None = None,
        config_home: Path | None = None,
        *,
        longrun_state_dir: Path | None = None,
        bound_project: str | None = None,
        profile: str | None = None,
    ):
        os.umask(0o077)
        default_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "codex-project-memory"
        default_config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "codex-project-memory"
        self.data_home = data_home or Path(os.environ.get("PROJECT_MEMORY_HOME", default_data_home))
        self.config_home = config_home or Path(os.environ.get("PROJECT_MEMORY_CONFIG_HOME", default_config_home))
        self.registry_path = self.data_home / "registry.json"
        default_longrun_state = Path(
            os.environ.get(
                "PROJECT_MEMORY_LONGRUN_STATE_DIR",
                Path.home() / ".local/state/codex-longrun",
            )
        )
        self.longrun_state_dir = (longrun_state_dir or default_longrun_state).expanduser().resolve()
        self.metrics = MetricsStore(self.data_home / "usage.sqlite3", SERVER_VERSION)
        selected_profile = (profile or os.environ.get("PROJECT_MEMORY_PROFILE", "admin")).strip().casefold()
        if selected_profile not in PROFILE_VALUES:
            raise MemoryError(
                "PROJECT_MEMORY_PROFILE must be lean, compat, or admin",
                code="invalid_profile",
                category="configuration",
                phase="initialize",
            )
        self.profile = selected_profile
        selected_project = bound_project or os.environ.get("PROJECT_MEMORY_PROJECT")
        self.bound_project = normalize_project_key(selected_project) if selected_project else None
        self._known_schema2: set[str] = set()

    def _secure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    @staticmethod
    def _bounded_env_int(
        names: tuple[str, ...],
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        raw = next((os.environ[name] for name in names if os.environ.get(name)), None)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise MemoryError(
                f"{names[0]} must be an integer",
                code="longrun_secret_config_invalid",
                category="configuration",
                phase="stage_longrun_secret",
            ) from exc
        if not minimum <= value <= maximum:
            raise MemoryError(
                f"{names[0]} must be between {minimum} and {maximum}",
                code="longrun_secret_config_invalid",
                category="configuration",
                phase="stage_longrun_secret",
            )
        return value

    @staticmethod
    def _secure_owned_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise MemoryError(
                "Longrun secret storage is not a real directory",
                code="longrun_secret_storage_invalid",
                category="security",
                phase="stage_longrun_secret",
            )
        if details.st_uid != os.getuid():
            raise MemoryError(
                "Longrun secret storage has the wrong owner",
                code="longrun_secret_storage_invalid",
                category="security",
                phase="stage_longrun_secret",
            )
        path.chmod(0o700)

    def _stage_longrun_stdin(self, payload: bytes) -> tuple[str, int]:
        ttl_sec = self._bounded_env_int(
            ("PROJECT_MEMORY_LONGRUN_SECRET_TTL_SEC", "LONGRUN_SECRET_TTL_SEC"),
            DEFAULT_LONGRUN_SECRET_TTL_SEC,
            30,
            3600,
        )
        max_bytes = self._bounded_env_int(
            (
                "PROJECT_MEMORY_LONGRUN_MAX_STDIN_SECRET_BYTES",
                "LONGRUN_MAX_STDIN_SECRET_BYTES",
            ),
            DEFAULT_LONGRUN_MAX_STDIN_SECRET_BYTES,
            1,
            1024 * 1024,
        )
        if not payload or len(payload) > max_bytes:
            raise MemoryError(
                "test-asset field has an invalid size for Longrun stdin",
                code="longrun_secret_size_invalid",
                category="security",
                phase="stage_longrun_secret",
            )
        self._secure_owned_dir(self.longrun_state_dir)
        directory = self.longrun_state_dir / "secrets"
        self._secure_owned_dir(directory)
        cutoff = time.time() - ttl_sec
        for candidate in directory.glob("*.stdin"):
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                continue
            if details.st_uid == os.getuid() and details.st_mtime <= cutoff:
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
        secret_id = uuid.uuid4().hex
        path = directory / f"{secret_id}.stdin"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while staging Longrun stdin")
                view = view[written:]
            os.fsync(descriptor)
            details = os.fstat(descriptor)
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise MemoryError(
                    "Longrun stdin staging permissions are not 0600",
                    code="longrun_secret_storage_invalid",
                    category="security",
                    phase="stage_longrun_secret",
                )
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(descriptor)
        return secret_id, ttl_sec

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
        source_projects = registry.setdefault("projects", {})
        source_roots = registry.get("roots", {}) if version >= 3 else {}
        if version >= 3 and not isinstance(source_roots, dict):
            raise MemoryError(
                "invalid project-memory root index",
                code="registry_invalid",
                category="configuration",
                phase="registry",
            )

        projects: dict[str, dict[str, Any]] = {}
        roots: dict[str, dict[str, Any]] = {}
        used_keys: set[str] = set()
        source_items = sorted(source_projects.items())
        for stored_key, entry in source_items:
            if not isinstance(entry, dict):
                raise MemoryError(
                    f"invalid registry entry: {stored_key}",
                    code="registry_invalid",
                    category="configuration",
                    phase="registry",
                )
            project_id = entry.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                raise MemoryError(
                    f"registry entry has no project id: {stored_key}",
                    code="registry_invalid",
                    category="configuration",
                    phase="registry",
                )
            if version >= 3 and stored_key != project_id:
                raise MemoryError(
                    f"registry project-id mismatch: {stored_key}",
                    code="registry_invalid",
                    category="configuration",
                    phase="registry",
                )
            root = entry.get("project_root") or (stored_key if version < 3 else None)
            if not isinstance(root, str) or not root:
                raise MemoryError(
                    f"registry entry has no project root: {stored_key}",
                    code="registry_invalid",
                    category="configuration",
                    phase="registry",
                )
            entry["project_root"] = root
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
            if project_id in projects:
                raise MemoryError(
                    f"duplicate registry project id: {project_id}",
                    code="registry_invalid",
                    category="configuration",
                    phase="registry",
                )
            projects[project_id] = entry
            roots.setdefault(root, {"default_project_id": None, "project_ids": []})[
                "project_ids"
            ].append(project_id)

        for root, index in roots.items():
            previous = source_roots.get(root, {}) if isinstance(source_roots.get(root, {}), dict) else {}
            previous_ids = previous.get("project_ids", [])
            if not isinstance(previous_ids, list):
                previous_ids = []
            grouped_ids = set(index["project_ids"])
            ordered_ids = [
                project_id
                for project_id in previous_ids
                if isinstance(project_id, str) and project_id in grouped_ids
            ]
            ordered_ids.extend(
                project_id
                for project_id in index["project_ids"]
                if project_id not in ordered_ids
            )
            previous_default = previous.get("default_project_id")
            if previous_default not in grouped_ids:
                previous_default = ordered_ids[0]
            index["default_project_id"] = previous_default
            index["project_ids"] = ordered_ids

        registry["projects"] = projects
        registry["roots"] = roots
        registry["schema_version"] = REGISTRY_SCHEMA_VERSION
        return registry

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"schema_version": REGISTRY_SCHEMA_VERSION, "projects": {}, "roots": {}}
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
        return registry["projects"].get(project_id)

    def _entry_by_key(self, registry: dict[str, Any], project_key: str) -> dict[str, Any] | None:
        folded = normalize_project_key(project_key).casefold()
        return next((entry for entry in registry["projects"].values() if entry["project_key"].casefold() == folded), None)

    def _entries_for_root(self, registry: dict[str, Any], root: str) -> list[dict[str, Any]]:
        index = registry["roots"].get(root)
        if not index:
            return []
        return [registry["projects"][project_id] for project_id in index["project_ids"]]

    def _describe_entry(self, registry: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        parent = self._entry_by_id(registry, entry.get("parent_project_id"))
        children = sorted(
            child["project_key"]
            for child in registry["projects"].values()
            if child.get("parent_project_id") == entry["project_id"]
        )
        root_index = registry["roots"].get(entry["project_root"], {})
        same_root_projects = sorted(
            (candidate["project_key"] for candidate in self._entries_for_root(registry, entry["project_root"])),
            key=str.casefold,
        )
        return {
            **entry,
            "parent_project": parent["project_key"] if parent else None,
            "children": children,
            "is_meta_project": bool(children),
            "same_root_projects": same_root_projects,
            "is_default_for_root": root_index.get("default_project_id") == entry["project_id"],
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
        index = registry["roots"].get(root)
        if not index:
            raise MemoryError(
                f"project is not enrolled: {root}",
                code="project_not_enrolled",
                category="configuration",
                phase="resolve_project",
            )
        entry = self._entry_by_id(registry, index.get("default_project_id"))
        if not entry or entry.get("project_root") != root:
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
        parent_project: str | None,
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
        if parent_root is not None and parent_project is not None:
            raise MemoryError(
                "parent_root and parent_project are mutually exclusive",
                code="invalid_parent",
                category="configuration",
                phase="enroll",
            )

        registry = self._load_registry()
        root_entries = self._entries_for_root(registry, root)
        default_existing = self._resolve_root_in_registry(registry, root) if root_entries else None
        requested_key = normalize_project_key(project_key) if project_key is not None else None
        keyed_existing = self._entry_by_key(registry, requested_key) if requested_key else None
        if keyed_existing and keyed_existing["project_root"] != root:
            raise MemoryError(
                f"project key is already enrolled: {requested_key}",
                code="project_key_conflict",
                category="configuration",
                phase="enroll",
            )
        existing = keyed_existing if requested_key is not None else default_existing
        parent = None
        parent_project_id = existing.get("parent_project_id") if existing else None
        if parent_project is not None:
            parent = self._entry_by_key(registry, parent_project)
            if not parent:
                raise MemoryError(
                    f"parent project is not enrolled: {normalize_project_key(parent_project)}",
                    code="parent_not_enrolled",
                    category="configuration",
                    phase="enroll",
                )
        elif parent_root is not None:
            parent = self._resolve_root_in_registry(registry, parent_root)
        if parent:
            parent_path = Path(parent["project_root"])
            try:
                Path(root).relative_to(parent_path)
            except ValueError as exc:
                raise MemoryError(
                    "subproject root must be inside its parent project root",
                    code="outside_parent",
                    category="configuration",
                    phase="enroll",
                ) from exc
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

        if requested_key is not None:
            key = existing["project_key"] if existing else requested_key
        elif (parent_root is not None or parent_project is not None) and parent:
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
        if conflicting and (not existing or conflicting["project_id"] != existing["project_id"]):
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
            if parent is None:
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
        if root_entries and not existing:
            identity = f"{identity}\n{key.casefold()}"
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "project"
        project_id = existing["project_id"] if existing else f"{slug}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
        ancestor = parent
        visited_parent_ids: set[str] = set()
        while ancestor:
            ancestor_id = ancestor["project_id"]
            if ancestor_id == project_id or ancestor_id in visited_parent_ids:
                raise MemoryError(
                    "project parent relationship would create a cycle",
                    code="invalid_parent",
                    category="configuration",
                    phase="enroll",
                )
            visited_parent_ids.add(ancestor_id)
            ancestor = self._entry_by_id(registry, ancestor.get("parent_project_id"))
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
        registry["projects"][project_id] = entry
        root_index = registry["roots"].setdefault(
            root,
            {"default_project_id": project_id, "project_ids": []},
        )
        if project_id not in root_index["project_ids"]:
            root_index["project_ids"].append(project_id)
        if not root_index.get("default_project_id"):
            root_index["default_project_id"] = project_id
        return registry, entry

    def preview_enrollment(
        self,
        project_root: str,
        project_name: str | None = None,
        allow_test_secrets: bool | None = None,
        parent_root: str | None = None,
        project_key: str | None = None,
        parent_project: str | None = None,
    ) -> dict[str, Any]:
        registry, entry = self._prepare_enrollment(
            project_root,
            project_name,
            allow_test_secrets,
            parent_root,
            project_key,
            parent_project,
        )
        return self._describe_entry(registry, entry)

    def enroll(
        self,
        project_root: str,
        project_name: str | None = None,
        allow_test_secrets: bool | None = None,
        parent_root: str | None = None,
        project_key: str | None = None,
        parent_project: str | None = None,
    ) -> dict[str, Any]:
        registry, entry = self._prepare_enrollment(
            project_root,
            project_name,
            allow_test_secrets,
            parent_root,
            project_key,
            parent_project,
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
        if self.bound_project:
            entry = self.resolve_project_key(self.bound_project)
            if project is not None and normalize_project_key(str(project)).casefold() != self.bound_project.casefold():
                raise MemoryError(
                    "project-bound server cannot access another project",
                    code="bound_project_mismatch",
                    category="security",
                    phase="resolve_project",
                )
            if project_root is not None:
                try:
                    selected_root = canonical(require_text(args, "project_root", 4096))
                except (OSError, RuntimeError) as exc:
                    raise MemoryError(
                        f"invalid project root: {exc}",
                        code="invalid_project_root",
                        category="configuration",
                        phase="resolve_project",
                    ) from exc
                if selected_root != entry["project_root"]:
                    raise MemoryError(
                        "project-bound server cannot access another project root",
                        code="bound_project_mismatch",
                        category="security",
                        phase="resolve_project",
                    )
            return entry
        if project is not None:
            entry = self.resolve_project_key(require_text(args, "project", 1000))
            if project_root is not None:
                try:
                    selected_root = canonical(require_text(args, "project_root", 4096))
                except (OSError, RuntimeError) as exc:
                    raise MemoryError(
                        f"invalid project root: {exc}",
                        code="invalid_project_root",
                        category="configuration",
                        phase="resolve_project",
                    ) from exc
                if selected_root != entry["project_root"]:
                    raise MemoryError(
                        "project and project_root identify different project roots",
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

    def _stored_content_schema_version(self, entry: dict[str, Any]) -> int:
        source = self._db_path(entry)
        if not source.exists():
            return 0
        connection = sqlite3.connect(source)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_metadata'"
            ).fetchone()
            if not table:
                return 1
            row = connection.execute(
                "SELECT value FROM memory_metadata WHERE key='schema_version'"
            ).fetchone()
            return int(row[0]) if row else 1
        finally:
            connection.close()

    def _connect(self, entry: dict[str, Any], *, backup_before_migration: bool = True) -> sqlite3.Connection:
        db_path = self._db_path(entry)
        self._secure_dir(db_path.parent)
        stored_version = MEMORY_SCHEMA_VERSION if entry["project_id"] in self._known_schema2 else self._stored_content_schema_version(entry)
        if backup_before_migration and 0 < stored_version < MEMORY_SCHEMA_VERSION:
            self._raw_backup(entry, "pre-schema2")
        connection = sqlite3.connect(db_path)
        db_path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            self._ensure_content_schema(connection)
            self._known_schema2.add(entry["project_id"])
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    def _ensure_content_schema(self, connection: sqlite3.Connection) -> None:
        """Create or transactionally migrate one project database to schema 2."""
        try:
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
                CREATE TABLE IF NOT EXISTS memory_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS record_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    importance TEXT NOT NULL DEFAULT 'ordinary',
                    summary TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(record_id) REFERENCES records(id)
                );
                CREATE INDEX IF NOT EXISTS record_events_record_time_idx
                    ON record_events(record_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS record_events_record_importance_idx
                    ON record_events(record_id,importance,created_at DESC);
                CREATE TABLE IF NOT EXISTS record_relations (
                    source_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,relation,target_id),
                    FOREIGN KEY(source_id) REFERENCES records(id),
                    FOREIGN KEY(target_id) REFERENCES records(id)
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM memory_metadata WHERE key='schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 1
            if version > MEMORY_SCHEMA_VERSION:
                raise MemoryError(
                    f"project database schema {version} is newer than supported schema {MEMORY_SCHEMA_VERSION}",
                    code="schema_too_new",
                    category="storage",
                    phase="database_migration",
                )
            columns = self._table_columns(connection, "records")
            additions = {
                "importance": "TEXT NOT NULL DEFAULT 'durable'",
                "confidence": "TEXT NOT NULL DEFAULT 'medium'",
                "card_json": "TEXT",
                "action_json": "TEXT",
                "evidence_json": "TEXT",
                "card_bytes": "INTEGER NOT NULL DEFAULT 0",
                "action_bytes": "INTEGER NOT NULL DEFAULT 0",
                "evidence_bytes": "INTEGER NOT NULL DEFAULT 0",
                "completeness_json": "TEXT NOT NULL DEFAULT '{}'",
                "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
                "applicability_json": "TEXT NOT NULL DEFAULT '{}'",
                "verified_at": "TEXT",
                "verified_commit": "TEXT",
                "watch_paths_json": "TEXT NOT NULL DEFAULT '[]'",
                "watch_blobs_json": "TEXT NOT NULL DEFAULT '{}'",
                "supersedes_id": "TEXT",
                "expires_at": "TEXT",
                "stable_key": "TEXT",
                "stale_state": "TEXT NOT NULL DEFAULT 'unknown'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE records ADD COLUMN {name} {definition}")
            connection.execute("CREATE INDEX IF NOT EXISTS records_kind_status_idx ON records(kind,status)")
            connection.execute("CREATE INDEX IF NOT EXISTS records_stable_key_idx ON records(kind,stable_key,status)")
            connection.execute("CREATE INDEX IF NOT EXISTS records_importance_idx ON records(status,importance)")
            connection.execute("CREATE INDEX IF NOT EXISTS records_expiry_idx ON records(status,expires_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS records_supersedes_idx ON records(supersedes_id)")
            if version < 2:
                self._migrate_content_v1_to_v2(connection)
            connection.execute(
                "INSERT INTO memory_metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(MEMORY_SCHEMA_VERSION),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _migrate_content_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        now = utc_now()
        rows = connection.execute("SELECT * FROM records ORDER BY created_at,id").fetchall()
        for row in rows:
            connection.execute(
                "INSERT OR REPLACE INTO revisions(record_id,revision,snapshot_json,changed_at) VALUES(?,?,?,?)",
                (row["id"], row["revision"], self._snapshot(row), now),
            )
            payload = json.loads(row["payload_json"])
            observations = payload.pop("observations", [])
            attempt_history = payload.pop("attempt_history", [])
            for observation in observations if isinstance(observations, list) else []:
                text = observation.get("text", "") if isinstance(observation, dict) else str(observation)
                created_at = observation.get("at", row["updated_at"]) if isinstance(observation, dict) else row["updated_at"]
                connection.execute(
                    "INSERT INTO record_events(record_id,event_type,importance,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "occurrence", "ordinary", text[:500], compact_json({"observation": text, "migrated": True}), created_at),
                )
            for attempt in attempt_history if isinstance(attempt_history, list) else []:
                detail = attempt if isinstance(attempt, dict) else {"observation": str(attempt)}
                event_type = "failed_attempt" if any(
                    key in detail for key in ("attempt", "why_it_failed", "do_not_repeat")
                ) else "occurrence"
                summary = str(detail.get("observed_result") or detail.get("text") or detail.get("observation") or "")
                connection.execute(
                    "INSERT INTO record_events(record_id,event_type,importance,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], event_type, "ordinary", summary[:500], compact_json({**detail, "migrated": True}), detail.get("at", row["updated_at"])),
                )
            if "final_steps" in payload and "steps" not in payload:
                payload["steps"] = payload.pop("final_steps")
            importance = "working" if row["kind"] in {"candidate", "checkpoint"} else "durable"
            connection.execute(
                "UPDATE records SET payload_json=?,importance=?,confidence='medium',stale_state='unknown' WHERE id=?",
                (compact_json(payload), importance, row["id"]),
            )
            connection.execute(
                "INSERT INTO record_events(record_id,event_type,importance,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                (row["id"], "migration", "ordinary", "Migrated to memory schema 2", compact_json({"from_schema": 1, "to_schema": 2}), now),
            )
        for row in rows:
            self._refresh_record(connection, row["id"])

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
        body_parts = [compact_json(payload)]
        if row["stable_key"]:
            body_parts.append(row["stable_key"])
        connection.execute(
            "INSERT INTO records_fts(record_id,title,summary,problem,context,body,tags) VALUES(?,?,?,?,?,?,?)",
            (record_id, row["title"], row["summary"], row["problem"], row["context"], " ".join(body_parts), tags),
        )

    @staticmethod
    def _event_rows(connection: sqlite3.Connection, record_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT id,event_type,importance,summary,detail_json,created_at "
            "FROM record_events WHERE record_id=? ORDER BY created_at DESC,id DESC",
            (record_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "importance": row["importance"],
                "summary": row["summary"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _refresh_record(self, connection: sqlite3.Connection, record_id: str) -> None:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise MemoryError("record not found", code="record_not_found", category="record", phase="materialize")
        payload = json.loads(row["payload_json"])
        provenance = json.loads(row["provenance_json"] or "{}")
        record = {**dict(row), "payload": payload, "provenance": provenance}
        views = build_record_views(record, self._event_rows(connection, record_id))
        view_payload = {
            "card": views.card,
            "action": views.action,
            "evidence": views.evidence,
            "completeness": views.completeness,
        }
        found = find_sensitive(view_payload)
        if found:
            raise MemoryError(
                f"credential-like value in materialized view {found}",
                code="sensitive_content_rejected",
                category="security",
                phase="materialize",
            )
        connection.execute(
            """UPDATE records SET card_json=?,action_json=?,evidence_json=?,card_bytes=?,
                      action_bytes=?,evidence_bytes=?,completeness_json=? WHERE id=?""",
            (
                compact_json(views.card),
                compact_json(views.action),
                compact_json(views.evidence),
                views.card_bytes,
                views.action_bytes,
                views.evidence_bytes,
                compact_json(views.completeness),
                record_id,
            ),
        )
        self._index(connection, record_id, payload)

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
            "importance": row["importance"], "confidence": row["confidence"],
            "verified_at": row["verified_at"], "verified_commit": row["verified_commit"],
            "supersedes_id": row["supersedes_id"], "expires_at": row["expires_at"],
            "stable_key": row["stable_key"], "staleness_state": row["stale_state"],
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
        schema_row = connection.execute("SELECT value FROM memory_metadata WHERE key='schema_version'").fetchone()
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
            "same_root_projects": described["same_root_projects"],
            "is_default_for_root": described["is_default_for_root"],
            "allow_test_secrets": entry["allow_test_secrets"],
            "counts": counts,
            "memory_schema_version": int(schema_row["value"]),
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

    @staticmethod
    def _token_budget(args: dict[str, Any]) -> tuple[int, int, int, int]:
        target_tokens = int(args.get("target_tokens", DEFAULT_TARGET_TOKENS))
        max_tokens = int(args.get("max_tokens", DEFAULT_MAX_TOKENS))
        if target_tokens < 128 or target_tokens > 100_000:
            raise MemoryError("target_tokens must be between 128 and 100000", code="invalid_argument")
        if max_tokens < target_tokens or max_tokens > 200_000:
            raise MemoryError("max_tokens must be at least target_tokens and at most 200000", code="invalid_argument")
        target_bytes = int(args.get("target_bytes", target_tokens * TOKEN_BYTE_RATIO))
        max_bytes = int(args.get("max_bytes", max_tokens * TOKEN_BYTE_RATIO))
        if target_bytes < 512 or target_bytes > max_bytes:
            raise MemoryError("target_bytes must be at least 512 and no greater than max_bytes", code="invalid_argument")
        if max_bytes > 2_000_000:
            raise MemoryError("max_bytes must not exceed 2000000", code="invalid_argument")
        return target_tokens, max_tokens, target_bytes, max_bytes

    @staticmethod
    def _estimated_tokens(byte_count: int) -> int:
        return math.ceil(byte_count / TOKEN_BYTE_RATIO)

    def _capture_git_context(self, entry: dict[str, Any], paths: list[str]) -> tuple[str | None, dict[str, str]]:
        root = entry["project_root"]
        commit = None
        blobs: dict[str, str] = {}
        try:
            commit_result = subprocess.run(
                ["git", "-C", root, "rev-parse", "HEAD"],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
            if commit_result.returncode == 0:
                commit = commit_result.stdout.strip() or None
            for path in paths:
                candidate = (Path(root) / path).resolve()
                try:
                    relative = candidate.relative_to(Path(root).resolve()).as_posix()
                except ValueError:
                    continue
                if not candidate.is_file():
                    blobs[relative] = "<missing>"
                    continue
                result = subprocess.run(
                    ["git", "-C", root, "hash-object", "--", relative],
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                blobs[relative] = result.stdout.strip() if result.returncode == 0 else "<unavailable>"
        except (OSError, subprocess.SubprocessError):
            return commit, blobs
        return commit, blobs

    def _effective_staleness(self, entry: dict[str, Any], row: sqlite3.Row) -> str:
        state = row["stale_state"] or "unknown"
        expires_at = row["expires_at"]
        if expires_at and expires_at <= utc_now():
            return "expired"
        paths = json.loads(row["watch_paths_json"] or "[]")
        stored_blobs = json.loads(row["watch_blobs_json"] or "{}")
        if paths and stored_blobs:
            _, current_blobs = self._capture_git_context(entry, paths)
            if current_blobs != stored_blobs:
                return "possibly_stale" if all(value != "<missing>" for value in current_blobs.values()) else "stale"
            return "fresh"
        return state if state in STALE_STATES else "unknown"

    def _recall_entries(self, entry: dict[str, Any], include_parent: bool, include_children: bool = False) -> list[dict[str, Any]]:
        entries = [entry]
        registry = self._load_registry()
        if include_parent and entry.get("parent_project_id"):
            parent = self._entry_by_id(registry, entry["parent_project_id"])
            if parent:
                entries.append(parent)
        if include_children:
            entries.extend(
                child for child in sorted(registry["projects"].values(), key=lambda value: value["project_key"].casefold())
                if child.get("parent_project_id") == entry["project_id"] and child["project_id"] not in {value["project_id"] for value in entries}
            )
        return entries

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"[\w./:+@-]+", query.casefold(), re.UNICODE)))[:24]

    def _search_candidates(
        self,
        entry: dict[str, Any],
        query: str,
        *,
        active_scope: bool,
        kinds: set[str] | None,
        importance_filter: set[str] | None,
        include_cold: bool,
        task_type: str,
        paths: list[str],
    ) -> list[dict[str, Any]]:
        terms = self._query_terms(query)
        if not terms:
            return []
        connection = self._connect(entry)
        feedback_scores: dict[str, float] = {}
        for feedback in connection.execute(
            "SELECT record_id,detail_json FROM record_events WHERE event_type='feedback' ORDER BY created_at"
        ):
            try:
                outcome = json.loads(feedback["detail_json"]).get("outcome")
            except (TypeError, json.JSONDecodeError):
                continue
            feedback_scores[feedback["record_id"]] = feedback_scores.get(feedback["record_id"], 0.0) + {
                "reused": 3.0,
                "helpful": 1.5,
                "not_applicable": -5.0,
                "stale": -20.0,
            }.get(outcome, 0.0)
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        exact_rows = connection.execute(
            "SELECT * FROM records WHERE status='active' AND (lower(stable_key)=? OR lower(title)=?) LIMIT 20",
            (query.casefold().strip(), query.casefold().strip()),
        ).fetchall()
        for row in exact_rows:
            rows.append(row)
            seen.add(row["id"])
        quoted = [term.replace('"', '""') for term in terms]
        for joiner in (" AND ", " OR "):
            expression = joiner.join(f'"{term}"*' for term in quoted)
            try:
                found = connection.execute(
                    """SELECT r.*,bm25(records_fts,0.0,10.0,5.0,8.0,4.0,2.0,1.0) AS lexical_rank
                       FROM records_fts f JOIN records r ON r.id=f.record_id
                       WHERE records_fts MATCH ? AND r.status='active'
                       ORDER BY lexical_rank,r.updated_at DESC LIMIT 50""",
                    (expression,),
                ).fetchall()
            except sqlite3.OperationalError:
                found = []
            for row in found:
                if row["id"] not in seen:
                    rows.append(row)
                    seen.add(row["id"])
            if rows:
                break
        candidates: list[dict[str, Any]] = []
        continuation = task_type == "continuation" or any(term in {"continue", "resume", "continuation", "продолжить", "продолжай"} for term in terms)
        query_folded = query.casefold().strip()
        for row in rows:
            if kinds and row["kind"] not in kinds:
                continue
            if importance_filter and row["importance"] not in importance_filter:
                continue
            if row["importance"] == "cold" and not include_cold:
                continue
            if row["kind"] == "checkpoint" and not continuation:
                continue
            staleness = self._effective_staleness(entry, row)
            if staleness in {"superseded", "expired"} and not include_cold:
                continue
            searchable = " ".join(
                str(value) for value in (
                    row["stable_key"] or "",
                    row["title"],
                    row["summary"],
                    row["problem"],
                    row["context"],
                    row["payload_json"],
                )
            ).casefold()
            matched = [term for term in terms if term in searchable]
            matched_paths = [path for path in paths if path.casefold() in searchable]
            minimum_matches = 1 if len(terms) <= 2 else math.ceil(len(terms) * 0.5)
            if len(matched) < minimum_matches and not matched_paths:
                continue
            score = float(len(matched) * 4)
            reasons = matched[:8]
            for path in matched_paths:
                score += 20
                reasons.append(path)
            if row["stable_key"] and row["stable_key"].casefold() == query_folded:
                score += 100
                reasons.insert(0, "stable_key")
            if row["title"].casefold() == query_folded:
                score += 60
                reasons.insert(0, "exact_title")
            score += {"verified": 24, "high": 12, "medium": 4, "low": 0}.get(row["confidence"], 0)
            score += {"pinned": 18, "durable": 9, "working": 1, "cold": -20}.get(row["importance"], 0)
            score += 8 if active_scope else 0
            score += {"fresh": 8, "unknown": 0, "possibly_stale": -12, "stale": -40}.get(staleness, -50)
            score += max(-30.0, min(12.0, feedback_scores.get(row["id"], 0.0)))
            card = json.loads(row["card_json"] or "{}")
            card["match_reason"] = reasons
            card["staleness_state"] = staleness
            candidates.append(
                {
                    "entry": entry,
                    "row": row,
                    "card": card,
                    "score": score,
                    "staleness": staleness,
                    "completeness": json.loads(row["completeness_json"] or "{}"),
                    "matched_terms": len(matched),
                    "query_terms": len(terms),
                }
            )
        connection.close()
        return candidates

    @staticmethod
    def _deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        kind_counts: dict[str, int] = {}
        for candidate in sorted(candidates, key=lambda value: (-value["score"], value["row"]["updated_at"]), reverse=False):
            row = candidate["row"]
            identity = row["stable_key"] or row["fingerprint"] or row["id"]
            key = f"{row['kind']}:{str(identity).casefold()}"
            if key in seen:
                continue
            if kind_counts.get(row["kind"], 0) >= 2:
                continue
            seen.add(key)
            kind_counts[row["kind"]] = kind_counts.get(row["kind"], 0) + 1
            result.append(candidate)
        return result

    def _explicit_conflicts(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        entry = candidate["entry"]
        row = candidate["row"]
        connection = self._connect(entry)
        conflicts = connection.execute(
            """SELECT r.card_json FROM record_relations rel JOIN records r ON r.id=rel.target_id
               WHERE rel.source_id=? AND rel.relation='contradicts' AND r.status='active'""",
            (row["id"],),
        ).fetchall()
        connection.close()
        return [json.loads(value["card_json"] or "{}") for value in conflicts]

    def _apply_folded_feedback(self, entry: dict[str, Any], dismiss: Any) -> None:
        if dismiss is None:
            return
        if not isinstance(dismiss, list) or len(dismiss) > 20:
            raise MemoryError("dismiss must be a list with at most 20 items", code="invalid_argument")
        for item in dismiss:
            if not isinstance(item, dict):
                raise MemoryError("dismiss entries must be objects", code="invalid_argument")
            self.mark_used({"project": entry["project_key"], "record_id": item.get("record_id"), "outcome": item.get("outcome")})

    def recall(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        query = require_text(args, "query", 4000)
        mode = str(args.get("mode", "auto")).casefold()
        if mode not in {"compact", "balanced", "deep", "auto"}:
            raise MemoryError("mode must be compact, balanced, deep, or auto", code="invalid_argument")
        target_tokens, max_tokens, target_bytes, max_bytes = self._token_budget(args)
        include_parent = bool(args.get("include_parent", True))
        include_children = bool(args.get("include_children", False))
        include_evidence = str(args.get("include_evidence", "auto")).casefold()
        if include_evidence not in {"auto", "true", "false"}:
            raise MemoryError("include_evidence must be auto, true, or false", code="invalid_argument")
        task_type = str(args.get("task_type", "")).casefold()
        risk = str(args.get("risk", "normal")).casefold()
        kinds_value = args.get("kinds")
        kinds = set(string_list(kinds_value, "kinds", 20)) if kinds_value is not None else None
        if kinds and not kinds.issubset(RECORD_KINDS):
            raise MemoryError("kinds contains an unsupported record kind", code="invalid_argument")
        importance_value = args.get("importance")
        importance_filter = set(string_list(importance_value, "importance", 4)) if importance_value is not None else None
        if importance_filter and not importance_filter.issubset(IMPORTANCE_VALUES):
            raise MemoryError("importance contains an unsupported tier", code="invalid_argument")
        paths = string_list(args.get("paths"), "paths", 100)
        self._apply_folded_feedback(entry, args.get("dismiss"))
        searched_entries = self._recall_entries(entry, include_parent, include_children)
        candidates: list[dict[str, Any]] = []
        for index, selected in enumerate(searched_entries):
            candidates.extend(
                self._search_candidates(
                    selected,
                    query,
                    active_scope=index == 0,
                    kinds=kinds,
                    importance_filter=importance_filter,
                    include_cold=mode == "deep" and bool(args.get("include_cold", False)),
                    task_type=task_type,
                    paths=paths,
                )
            )
        candidates = self._deduplicate_candidates(candidates)
        high_risk = risk in {"high", "safety", "destructive", "credential", "production", "hardware"}
        architectural = task_type in {"architecture", "investigation"} or any(
            word in query.casefold() for word in ("architecture", "rationale", "alternatives", "why", "архитект", "почему", "истори")
        )
        top = candidates[0] if candidates else None
        conflicts = self._explicit_conflicts(top) if top else []
        direct_safe = False
        if top:
            kind = top["row"]["kind"]
            confidence = top["row"]["confidence"]
            direct_safe = (
                kind in {"solution", "constraint", "decision", "failure_pattern", "environment", "checkpoint"}
                and is_complete(kind, top["completeness"])
                and confidence in ({"verified"} if high_risk and kind == "solution" else {"high", "verified"})
                and top["staleness"] in {"fresh", "unknown"}
                and not conflicts
                and (len(candidates) == 1 or top["score"] - candidates[1]["score"] >= 8)
            )
        if mode == "auto":
            if high_risk or architectural or conflicts:
                mode_used = "deep"
                mode_reason = "risk_or_architectural_context"
            elif direct_safe and top and top["row"]["action_bytes"] <= target_bytes:
                mode_used = "compact"
                mode_reason = "complete_high_confidence_match"
            else:
                mode_used = "balanced"
                mode_reason = "ordinary_adaptive_recall"
        else:
            mode_used = mode
            mode_reason = "explicit_mode"
        limit = int(args.get("limit", MAXIMUM_CARDS if mode_used == "deep" else DEFAULT_CARDS))
        if limit < 1 or limit > MAXIMUM_CARDS:
            raise MemoryError(f"limit must be between 1 and {MAXIMUM_CARDS}", code="invalid_argument")
        result: dict[str, Any] = {
            "project": entry["project_key"],
            "searched_projects": [value["project_key"] for value in searched_entries],
            "mode_requested": mode,
            "mode_used": mode_used,
            "mode_reason": mode_reason,
            "confidence": top["row"]["confidence"] if top else "low",
            "direct_action": None,
            "cards": [],
            "alternatives": [],
            "conflicts": conflicts,
            "more_available": len(candidates) > limit,
        }
        reason = "no_results"
        if top and direct_safe:
            materialized_view = "evidence" if mode_used == "deep" else "action"
            action = json.loads(top["row"][f"{materialized_view}_json"] or "{}")
            action["staleness_state"] = top["staleness"]
            support: list[dict[str, Any]] = []
            use_evidence = include_evidence == "true" or (include_evidence == "auto" and mode_used in {"balanced", "deep"})
            if use_evidence:
                evidence = json.loads(top["row"]["evidence_json"] or "{}")
                if evidence.get("important_failures"):
                    support.append({"kind": "important_failures", "items": evidence["important_failures"]})
            if mode_used == "deep":
                for candidate in candidates[1:4]:
                    if candidate["row"]["kind"] != top["row"]["kind"]:
                        support.append(candidate["card"])
            direct = {"record": action, "support": support}
            if serialized_bytes(direct) <= max_bytes:
                result["direct_action"] = direct
                result["coverage"] = json.loads(top["row"]["completeness_json"] or "{}")
                reason = "direct_action"
            else:
                reason = "budget_requires_read"
                result["recommended_read"] = {"record_id": top["row"]["id"], "view": "action"}
        elif top:
            if conflicts:
                reason = "conflict"
            elif top["staleness"] not in {"fresh", "unknown"}:
                reason = "stale"
            elif not is_complete(top["row"]["kind"], top["completeness"]):
                reason = "incomplete_record"
            else:
                reason = "ambiguous_matches" if len(candidates) > 1 else "low_confidence"
            result["recommended_read"] = {"record_id": top["row"]["id"], "view": "evidence"}
        if result["direct_action"] is None:
            result["reason"] = reason
            result["cards"] = [candidate["card"] for candidate in candidates[:limit]]
        else:
            result["alternatives"] = [candidate["card"] for candidate in candidates[1:limit]]
        actual_bytes = serialized_bytes(result)
        while actual_bytes > max_bytes and result.get("alternatives"):
            result["alternatives"].pop()
            result["more_available"] = True
            actual_bytes = serialized_bytes(result)
        while actual_bytes > max_bytes and len(result.get("cards", [])) > 1:
            result["cards"].pop()
            result["more_available"] = True
            actual_bytes = serialized_bytes(result)
        result["budget"] = {
            "target_tokens": target_tokens,
            "max_tokens": max_tokens,
            "target_bytes": target_bytes,
            "max_bytes": max_bytes,
            "actual_bytes": actual_bytes,
            "estimated_tokens": self._estimated_tokens(actual_bytes),
            "target_exceeded_for_quality": actual_bytes > target_bytes,
            "maximum_reached": actual_bytes >= max_bytes,
        }
        return result

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        record_id = require_text(args, "record_id", 100)
        view = str(args.get("view", "action")).casefold()
        if view not in {"card", "action", "evidence", "full"}:
            raise MemoryError("view must be card, action, evidence, or full", code="invalid_argument")
        sections = string_list(args.get("sections"), "sections", 100)
        _, _, _, max_bytes = self._token_budget({
            "target_tokens": min(int(args.get("max_tokens", DEFAULT_MAX_TOKENS)), DEFAULT_TARGET_TOKENS),
            "max_tokens": int(args.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "target_bytes": min(int(args.get("max_bytes", DEFAULT_MAX_TOKENS * TOKEN_BYTE_RATIO)), DEFAULT_TARGET_TOKENS * TOKEN_BYTE_RATIO),
            "max_bytes": int(args.get("max_bytes", int(args.get("max_tokens", DEFAULT_MAX_TOKENS)) * TOKEN_BYTE_RATIO)),
        })
        connection = self._connect(entry)
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            connection.close()
            raise MemoryError("record not found", code="record_not_found", category="record", phase="read_record")
        staleness = self._effective_staleness(entry, row)
        if view in {"card", "action", "evidence"}:
            result = json.loads(row[f"{view}_json"] or "{}")
            if sections and view not in sections and "action" not in sections:
                selected = {key: result[key] for key in ("id", "kind", "title") if key in result}
                for section in sections:
                    if section in result:
                        selected[section] = result[section]
                result = selected
            result["staleness_state"] = staleness
            result["coverage"] = json.loads(row["completeness_json"] or "{}")
            event_count = connection.execute("SELECT count(*) FROM record_events WHERE record_id=?", (record_id,)).fetchone()[0]
            revision_count = connection.execute("SELECT count(*) FROM revisions WHERE record_id=?", (record_id,)).fetchone()[0]
            audit_count = connection.execute("SELECT count(*) FROM audit WHERE record_id=?", (record_id,)).fetchone()[0]
            result["more_available"] = bool(event_count or revision_count or audit_count)
            result["omitted"] = {"events": event_count, "revisions": revision_count, "audit_events": audit_count}
        else:
            public = self._public_record(row)
            events = self._event_rows(connection, record_id)
            relations = [dict(value) for value in connection.execute(
                "SELECT source_id,relation,target_id,created_at FROM record_relations WHERE source_id=? OR target_id=? ORDER BY created_at",
                (record_id, record_id),
            )]
            revisions = [dict(value) for value in connection.execute(
                "SELECT record_id,revision,snapshot_json,changed_at FROM revisions WHERE record_id=? ORDER BY revision",
                (record_id,),
            )]
            audit = [dict(value) for value in connection.execute(
                "SELECT id,action,record_id,detail_json,created_at FROM audit WHERE record_id=? ORDER BY id",
                (record_id,),
            )]
            result = {
                "record": public,
                "events": events,
                "relations": relations,
                "revisions": revisions,
                "audit": audit,
                "coverage": json.loads(row["completeness_json"] or "{}"),
                "staleness_state": staleness,
                "more_available": False,
                "omitted": {},
            }
            if sections:
                mapping = {"record": "record", "action": "record", "events": "events", "relations": "relations", "revisions": "revisions", "audit": "audit"}
                requested = {mapping[section] for section in sections if section in mapping}
                for section in ("record", "events", "relations", "revisions", "audit"):
                    if section not in requested:
                        removed = 1 if section == "record" else len(result[section])
                        result[section] = {} if section == "record" else []
                        result["omitted"][section] = removed
                        result["more_available"] = result["more_available"] or bool(removed)
            for section in ("audit", "revisions", "events"):
                if serialized_bytes(result) <= max_bytes:
                    break
                removed = len(result[section])
                result[section] = []
                result["omitted"][section] = removed
                result["more_available"] = True
        connection.close()
        result["project"] = entry["project_key"]
        actual_bytes = serialized_bytes(result)
        result["budget"] = {"actual_bytes": actual_bytes, "estimated_tokens": self._estimated_tokens(actual_bytes), "max_bytes": max_bytes}
        return result

    @staticmethod
    def _remember_payload(args: dict[str, Any], kind: str) -> dict[str, Any]:
        shared = (
            "applicability", "steps", "constraints", "warnings", "verification", "outcome",
            "versions", "devices", "platforms", "preconditions", "postconditions",
            "invalidates_when", "tags", "scope", "rationale",
        )
        by_kind = {
            "constraint": ("statement", "reason", "scope", "severity", "conditions"),
            "decision": ("decision", "reason", "alternatives_considered", "rejected_reasons", "consequences", "revisit_when"),
            "failure_pattern": ("symptom", "attempt", "observed_result", "why_it_failed", "do_not_repeat"),
            "environment": ("fact", "scope", "source", "confidence"),
            "checkpoint": ("goal", "completed", "current_state", "next_steps", "blockers", "branch", "head_commit", "touched_paths", "expires_at"),
        }
        payload: dict[str, Any] = {}
        for name in (*shared, *by_kind.get(kind, ())):
            if name in args:
                payload[name] = args[name]
        if "action" in args and "steps" not in payload:
            payload["steps"] = args["action"] if isinstance(args["action"], list) else [args["action"]]
        return payload

    @staticmethod
    def _validate_watch_paths(value: Any) -> list[str]:
        paths = string_list(value, "watch_paths", 100)
        blocked = re.compile(r"(^|/)(?:\.env(?:\..*)?|id_[a-z0-9_-]+|.*\.(?:pem|key|p12|pfx))$", re.I)
        for path in paths:
            normalized = path.replace("\\", "/")
            if normalized.startswith("/") or ".." in Path(normalized).parts or blocked.search(normalized):
                raise MemoryError(
                    "watch_paths must be relative non-secret project paths",
                    code="unsafe_watch_path",
                    category="security",
                    phase="validate_content",
                )
        return paths

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        record_id: str,
        event_type: str,
        importance: str,
        summary: str,
        detail: dict[str, Any],
        *,
        created_at: str | None = None,
    ) -> None:
        if importance not in {"ordinary", "important", "critical"}:
            raise MemoryError("event importance must be ordinary, important, or critical", code="invalid_argument")
        found = find_sensitive(detail)
        if found:
            raise MemoryError(
                f"credential-like value in event {found}",
                code="sensitive_content_rejected",
                category="security",
                phase="validate_content",
            )
        connection.execute(
            "INSERT INTO record_events(record_id,event_type,importance,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, event_type, importance, summary[:500], compact_json(detail), created_at or utc_now()),
        )

    def remember(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        operation = str(args.get("operation", "upsert")).casefold()
        if operation == "feedback":
            return self.mark_used(args)
        connection = self._connect(entry)
        now = utc_now()
        try:
            if operation == "failed_attempt":
                record_id = require_text(args, "record_id", 100)
                detail = {
                    name: require_text(args, name)
                    for name in ("attempt", "observed_result", "why_it_failed", "do_not_repeat")
                }
                row = connection.execute("SELECT * FROM records WHERE id=? AND status='active'", (record_id,)).fetchone()
                if not row:
                    raise MemoryError("active record not found", code="record_not_found", category="record", phase="remember")
                self._insert_event(
                    connection,
                    record_id,
                    "failed_attempt",
                    str(args.get("importance", "important")).casefold(),
                    detail["observed_result"],
                    detail,
                )
                self._refresh_record(connection, record_id)
                self._audit(connection, "remember_failed_attempt", record_id, {"importance": args.get("importance", "important")})
                connection.commit()
                return {"project": entry["project_key"], "record_id": record_id, "event": "failed_attempt"}

            if operation == "occurrence":
                problem = require_text(args, "problem")
                payload = self._remember_payload(args, "candidate")
                steps = payload.get("steps", [])
                if not isinstance(steps, list) or not steps:
                    raise MemoryError("action or steps must contain at least one item", code="invalid_argument")
                stable_key = str(args.get("stable_key") or normalize_fingerprint(problem, compact_json(steps))).strip()
                if len(stable_key) > 500:
                    raise MemoryError("stable_key exceeds 500 characters", code="invalid_argument")
                context = str(args.get("context", "")).strip()
                observation = str(args.get("observation", "")).strip()
                ordinary = {"problem": problem, "payload": payload, "context": context, "observation": observation}
                found = find_sensitive(ordinary)
                if found:
                    raise MemoryError(
                        f"credential-like value in {found}",
                        code="sensitive_content_rejected",
                        category="security",
                        phase="validate_content",
                    )
                row = connection.execute(
                    "SELECT * FROM records WHERE stable_key=? AND kind IN ('candidate','solution') AND status='active' ORDER BY kind='solution' DESC LIMIT 1",
                    (stable_key,),
                ).fetchone()
                if row:
                    record_id = row["id"]
                    merged = json.loads(row["payload_json"])
                    for key, value in payload.items():
                        if value not in (None, "", []):
                            merged[key] = value
                    connection.execute(
                        "UPDATE records SET problem=?,context=?,payload_json=?,repetition_count=repetition_count+1,revision=revision+1,updated_at=? WHERE id=?",
                        (problem, context or row["context"], compact_json(merged), now, record_id),
                    )
                else:
                    record_id = str(uuid.uuid4())
                    title = str(args.get("title") or f"Candidate: {str(steps[0])[:120]}").strip()
                    connection.execute(
                        """INSERT INTO records(id,kind,title,summary,problem,context,payload_json,fingerprint,
                                   repetition_count,importance,confidence,stable_key,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (record_id, "candidate", title, observation, problem, context, compact_json(payload), normalize_fingerprint(problem, compact_json(steps)), 1, "working", "low", stable_key, now, now),
                    )
                self._insert_event(connection, record_id, "occurrence", "ordinary", observation, {"observation": observation, "verified": bool(args.get("verified", False))})
                if args.get("verified") is True:
                    verification = payload.get("verification", [])
                    self._insert_event(connection, record_id, "verification", "important", "Occurrence verified", {"verification": verification})
                self._save_revision(connection, record_id)
                self._refresh_record(connection, record_id)
                current = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                auto_finalized = False
                if (
                    current["kind"] == "candidate"
                    and current["repetition_count"] >= 2
                    and args.get("verified") is True
                    and is_complete("solution", json.loads(current["completeness_json"] or "{}"))
                ):
                    provenance = args.get("provenance") if isinstance(args.get("provenance"), dict) else {
                        "source_type": "user_verified",
                        "recorded_at": now,
                        "recorded_by": "codex",
                        "verification_method": "structured occurrence verification",
                    }
                    connection.execute(
                        "UPDATE records SET kind='solution',importance='durable',confidence='verified',provenance_json=?,verified_at=?,stale_state='fresh',revision=revision+1,updated_at=? WHERE id=?",
                        (compact_json(provenance), now, now, record_id),
                    )
                    self._refresh_record(connection, record_id)
                    self._save_revision(connection, record_id)
                    auto_finalized = True
                self._audit(connection, "remember_occurrence", record_id, {"auto_finalized": auto_finalized})
                connection.commit()
                result = self._public_record(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
                result.update({"project": entry["project_key"], "auto_finalized": auto_finalized, "eligible_to_finalize": result["repetition_count"] >= 2})
                return result

            if operation not in {"upsert", "finalize", "checkpoint"}:
                raise MemoryError("operation must be occurrence, upsert, finalize, checkpoint, failed_attempt, or feedback", code="invalid_argument")
            kind = "checkpoint" if operation == "checkpoint" else str(args.get("kind", "solution" if operation == "finalize" else "")).casefold()
            if kind not in RECORD_KINDS or kind in {"test_asset", "log_location", "candidate"}:
                raise MemoryError("kind is not supported by project_memory_remember", code="invalid_argument")
            stable_key = require_text(args, "stable_key", 500) if operation != "finalize" else str(args.get("stable_key", "")).strip()
            record_id_arg = args.get("record_id") or args.get("candidate_id")
            row = None
            if record_id_arg:
                row = connection.execute("SELECT * FROM records WHERE id=? AND status='active'", (str(record_id_arg),)).fetchone()
            elif stable_key:
                row = connection.execute("SELECT * FROM records WHERE kind=? AND stable_key=? AND status='active'", (kind, stable_key)).fetchone()
                if row is None and kind == "solution":
                    row = connection.execute("SELECT * FROM records WHERE kind='candidate' AND stable_key=? AND status='active'", (stable_key,)).fetchone()
            if operation == "finalize" and not row:
                raise MemoryError("active candidate not found", code="candidate_not_found", category="record", phase="finalize")
            if kind == "solution" and (not row or row["repetition_count"] < 2):
                raise MemoryError("solution needs at least two recorded occurrences", code="candidate_not_ready", category="workflow", phase="finalize")
            if kind == "solution" and row and row["kind"] == "candidate" and args.get("verified") is not True:
                raise MemoryError("candidate finalization requires verified=true", code="verification_required", category="workflow", phase="finalize")
            payload = self._remember_payload(args, kind)
            title = str(args.get("title") or (row["title"] if row else stable_key)).strip()
            problem = str(args.get("problem") or (row["problem"] if row else "")).strip()
            context = str(args.get("context") or (row["context"] if row else "")).strip()
            summary = str(args.get("summary") or args.get("outcome") or (row["summary"] if row else title)).strip()
            if row:
                existing_payload = json.loads(row["payload_json"])
                existing_payload.update(payload)
                payload = existing_payload
                if not stable_key:
                    stable_key = row["stable_key"] or row["fingerprint"] or row["id"]
            importance = str(args.get("importance") or (row["importance"] if row else ("working" if kind == "checkpoint" else "durable"))).casefold()
            confidence = str(args.get("confidence") or (row["confidence"] if row else ("verified" if args.get("verified") is True else "medium"))).casefold()
            if importance not in IMPORTANCE_VALUES or confidence not in CONFIDENCE_VALUES:
                raise MemoryError("invalid importance or confidence", code="invalid_argument")
            watch_paths = self._validate_watch_paths(args.get("watch_paths"))
            verified_commit, watch_blobs = self._capture_git_context(entry, watch_paths) if watch_paths else (args.get("verified_commit"), {})
            provenance = args.get("provenance") if isinstance(args.get("provenance"), dict) else {}
            if args.get("verified") is True and not provenance:
                provenance = {"source_type": "user_verified", "recorded_at": now, "recorded_by": "codex", "verification_method": "structured verification"}
            if confidence == "verified" and args.get("verified") is not True and not (row and row["confidence"] == "verified"):
                raise MemoryError("verified confidence requires verification evidence", code="verification_required", category="workflow", phase="remember")
            expires_at = str(args.get("expires_at") or payload.get("expires_at") or "").strip() or None
            ordinary = {"title": title, "problem": problem, "context": context, "summary": summary, "payload": payload, "provenance": provenance, "watch_paths": watch_paths}
            found = find_sensitive(ordinary)
            if found:
                raise MemoryError(f"credential-like value in {found}", code="sensitive_content_rejected", category="security", phase="validate_content")
            if row:
                record_id = row["id"]
                connection.execute(
                    """UPDATE records SET kind=?,title=?,summary=?,problem=?,context=?,payload_json=?,importance=?,
                               confidence=?,provenance_json=?,verified_at=?,verified_commit=?,watch_paths_json=?,
                               watch_blobs_json=?,expires_at=?,stable_key=?,stale_state=?,revision=revision+1,updated_at=? WHERE id=?""",
                    (kind, title, summary, problem, context, compact_json(payload), importance, confidence, compact_json(provenance), now if args.get("verified") is True else row["verified_at"], verified_commit, compact_json(watch_paths), compact_json(watch_blobs), expires_at, stable_key, "fresh" if args.get("verified") is True else row["stale_state"], now, record_id),
                )
            else:
                record_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO records(id,kind,title,summary,problem,context,payload_json,importance,confidence,
                               provenance_json,verified_at,verified_commit,watch_paths_json,watch_blobs_json,
                               expires_at,stable_key,stale_state,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (record_id, kind, title, summary, problem, context, compact_json(payload), importance, confidence, compact_json(provenance), now if args.get("verified") is True else None, verified_commit, compact_json(watch_paths), compact_json(watch_blobs), expires_at, stable_key, "fresh" if args.get("verified") is True else "unknown", now, now),
                )
            if args.get("verified") is True:
                self._insert_event(connection, record_id, "verification", "important", "Structured record verified", {"verification": payload.get("verification", [])})
            self._save_revision(connection, record_id)
            self._refresh_record(connection, record_id)
            refreshed = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if not is_complete(kind, json.loads(refreshed["completeness_json"] or "{}")):
                raise MemoryError(f"{kind} is missing required structured fields", code="incomplete_record", category="workflow", phase="remember")
            supersedes_id = str(args.get("supersedes_id") or "").strip()
            if supersedes_id:
                old = connection.execute("SELECT * FROM records WHERE id=? AND id<>?", (supersedes_id, record_id)).fetchone()
                if not old:
                    raise MemoryError("superseded record not found", code="record_not_found", category="record", phase="remember")
                connection.execute("UPDATE records SET importance='cold',stale_state='superseded',revision=revision+1,updated_at=? WHERE id=?", (now, supersedes_id))
                self._save_revision(connection, supersedes_id)
                connection.execute("UPDATE records SET supersedes_id=?,revision=revision+1,updated_at=? WHERE id=?", (supersedes_id, now, record_id))
                self._save_revision(connection, record_id)
                connection.execute("INSERT OR IGNORE INTO record_relations(source_id,relation,target_id,created_at) VALUES(?,?,?,?)", (record_id, "supersedes", supersedes_id, now))
                self._insert_event(connection, supersedes_id, "superseded", "important", "Superseded by a newer record", {"replacement_id": record_id})
                self._refresh_record(connection, supersedes_id)
            relations = args.get("relations", [])
            if relations:
                if not isinstance(relations, list) or len(relations) > 50:
                    raise MemoryError("relations must be a list with at most 50 entries", code="invalid_argument")
                allowed_relations = {"supports", "contradicts", "supersedes", "constrained_by", "caused_by", "applies_with", "related_to"}
                for relation in relations:
                    if not isinstance(relation, dict) or relation.get("relation") not in allowed_relations:
                        raise MemoryError("invalid record relation", code="invalid_argument")
                    target_id = str(relation.get("target_id", ""))
                    if not connection.execute("SELECT 1 FROM records WHERE id=?", (target_id,)).fetchone():
                        raise MemoryError("relation target not found", code="record_not_found", category="record", phase="remember")
                    connection.execute("INSERT OR IGNORE INTO record_relations(source_id,relation,target_id,created_at) VALUES(?,?,?,?)", (record_id, relation["relation"], target_id, now))
            self._refresh_record(connection, record_id)
            self._audit(connection, f"remember_{operation}", record_id, {"kind": kind, "verified": args.get("verified") is True})
            connection.commit()
            result = self._public_record(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
            result.update({"project": entry["project_key"], "coverage": json.loads(refreshed["completeness_json"] or "{}")})
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def report_stale(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self.resolve_project_args(args)
        record_id = require_text(args, "record_id", 100)
        reason = require_text(args, "reason")
        state = str(args.get("state", "stale")).casefold()
        if state not in {"possibly_stale", "stale"}:
            raise MemoryError("state must be possibly_stale or stale", code="invalid_argument")
        if find_sensitive(reason):
            raise MemoryError("staleness reason must not contain credentials", code="sensitive_content_rejected", category="security", phase="validate_content")
        connection = self._connect(entry)
        try:
            row = connection.execute("SELECT * FROM records WHERE id=? AND status='active'", (record_id,)).fetchone()
            if not row:
                raise MemoryError("active record not found", code="record_not_found", category="record", phase="report_stale")
            connection.execute("UPDATE records SET stale_state=?,revision=revision+1,updated_at=? WHERE id=?", (state, utc_now(), record_id))
            self._insert_event(connection, record_id, "stale_detected", "important", reason, {"reason": reason, "state": state})
            self._save_revision(connection, record_id)
            self._refresh_record(connection, record_id)
            self._audit(connection, "report_stale", record_id, {"state": state})
            connection.commit()
            return {"project": entry["project_key"], "record_id": record_id, "staleness_state": state, "preserved": True}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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

    def stage_test_asset_for_longrun(self, args: dict[str, Any]) -> dict[str, Any]:
        """Stage one encrypted test-asset field as a one-time Longrun stdin handle."""
        entry = self.resolve_project_args(args)
        if not entry.get("allow_test_secrets"):
            raise MemoryError(
                "test-secret access is not allowed for this project",
                code="test_secrets_disabled",
                category="security",
                phase="stage_longrun_secret",
            )
        record_id = require_text(args, "record_id", 100)
        secret_field = require_text(args, "secret_field", 200)
        append_newline = args.get("append_newline", True)
        if not isinstance(append_newline, bool):
            raise MemoryError("append_newline must be boolean", code="invalid_argument")
        connection = self._connect(entry)
        secret_id: str | None = None
        try:
            row = connection.execute(
                "SELECT * FROM records WHERE id=? AND status='active'",
                (record_id,),
            ).fetchone()
            if not row or row["kind"] != "test_asset" or row["secret_blob"] is None:
                raise MemoryError(
                    "active encrypted test asset not found",
                    code="test_asset_required",
                    category="security",
                    phase="stage_longrun_secret",
                )
            public_payload = json.loads(row["payload_json"])
            if public_payload.get("test_only") is not True:
                raise MemoryError(
                    "test asset is not explicitly test-only",
                    code="test_only_required",
                    category="security",
                    phase="stage_longrun_secret",
                )
            secret_fields = self._decrypt(entry, row["secret_blob"])
            if secret_field not in secret_fields:
                raise MemoryError(
                    "requested secret field is not present",
                    code="secret_field_not_found",
                    category="security",
                    phase="stage_longrun_secret",
                )
            value = secret_fields[secret_field]
            if not isinstance(value, (str, int, float, bool)):
                raise MemoryError(
                    "requested secret field is not a scalar",
                    code="secret_field_invalid",
                    category="security",
                    phase="stage_longrun_secret",
                )
            payload = str(value).encode("utf-8")
            if append_newline and not payload.endswith(b"\n"):
                payload += b"\n"
            secret_id, ttl_sec = self._stage_longrun_stdin(payload)
            self._audit(
                connection,
                "stage_test_asset_for_longrun",
                record_id,
                {
                    "approved_scope": "test_only",
                    "target": "longrun_stdin",
                    "secret_field_name": secret_field,
                    "secret_value_returned": False,
                },
            )
            connection.commit()
            return {
                "project": entry["project_key"],
                "record_id": record_id,
                "stdin_secret_id": secret_id,
                "expires_in_sec": ttl_sec,
                "single_use": True,
                "output_suppression_required": True,
                "secret_value_returned": False,
            }
        except Exception:
            connection.rollback()
            if secret_id is not None:
                try:
                    (self.longrun_state_dir / "secrets" / f"{secret_id}.stdin").unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            connection.close()

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
            payload = {"action": action, "tags": tags}
            connection.execute(
                """INSERT INTO records(id,kind,title,summary,problem,context,payload_json,fingerprint,
                           repetition_count,importance,confidence,stable_key,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, "candidate", f"Candidate: {action[:120]}", observation, problem, context, compact_json(payload), fingerprint, 1, "working", "low", fingerprint, now, now),
            )
            action_name = "create_candidate"
        connection.execute(
            "INSERT INTO record_events(record_id,event_type,importance,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "occurrence", "ordinary", observation[:500], compact_json({"observation": observation}) if observation else "{}", now),
        )
        self._save_revision(connection, record_id)
        self._refresh_record(connection, record_id)
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
        final_context = context or row["context"]
        payload = {
            "steps": final_steps,
            "applicability": ({"scope": final_context, "versions": [], "devices": []} if final_context else {}),
            "constraints": [],
            "warnings": [],
            "versions": [],
            "devices": [],
            "verification": [verification],
            "outcome": outcome,
            "tags": sorted(set(old_payload.get("tags", []) + tags)),
        }
        revision = row["revision"] + 1
        now = utc_now()
        provenance = {
            "source_type": "manual",
            "recorded_at": now,
            "recorded_by": "codex",
            "verification_method": verification,
        }
        connection.execute(
            """UPDATE records SET kind='solution',title=?,summary=?,context=?,payload_json=?,
                       importance='durable',confidence='verified',provenance_json=?,verified_at=?,
                       stale_state='fresh',revision=?,updated_at=? WHERE id=?""",
            (title, outcome, final_context, compact_json(payload), compact_json(provenance), now, revision, now, record_id),
        )
        connection.execute(
            "INSERT INTO record_events(record_id,event_type,importance,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "verification", "important", verification[:500], compact_json({"verification": verification, "outcome": outcome}), now),
        )
        self._save_revision(connection, record_id)
        self._refresh_record(connection, record_id)
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
        self._refresh_record(connection, record_id)
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
        self._refresh_record(connection, record_id)
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
        self._refresh_record(connection, record_id)
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
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            connection.close()
            raise MemoryError(
                "record not found",
                code="record_not_found",
                category="record",
                phase="mark_used",
            )
        self._insert_event(connection, record_id, "feedback", "ordinary", outcome, {"outcome": outcome})
        if outcome == "stale" and row["status"] == "active":
            connection.execute(
                "UPDATE records SET stale_state='stale',revision=revision+1,updated_at=? WHERE id=?",
                (utc_now(), record_id),
            )
            self._save_revision(connection, record_id)
            self._refresh_record(connection, record_id)
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

    def _raw_backup(self, entry: dict[str, Any], label: str = "memory") -> Path | None:
        source = self._db_path(entry)
        if not source.exists():
            return None
        backup_dir = source.parent / "backups"
        self._secure_dir(backup_dir)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = backup_dir / f"{label}-{stamp}.sqlite3"
        suffix = 1
        while destination.exists():
            destination = backup_dir / f"{label}-{stamp}-{suffix}.sqlite3"
            suffix += 1
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        destination.chmod(0o600)
        return destination

    def migrate_databases(self, project: str | None = None, all_projects: bool = False) -> dict[str, Any]:
        registry_backup = None
        stored_registry_version = REGISTRY_SCHEMA_VERSION
        if self.registry_path.exists():
            stored_registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
            stored_registry_version = int(stored_registry.get("schema_version", 1))
            if stored_registry_version < REGISTRY_SCHEMA_VERSION:
                backup_dir = self.data_home / "backups"
                self._secure_dir(backup_dir)
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                registry_backup = backup_dir / f"registry-pre-schema{REGISTRY_SCHEMA_VERSION}-{stamp}.json"
                shutil.copy2(self.registry_path, registry_backup)
                registry_backup.chmod(0o600)
        registry = self._load_registry()
        if stored_registry_version < REGISTRY_SCHEMA_VERSION:
            self._save_registry(registry)
        if all_projects:
            entries = sorted(registry["projects"].values(), key=lambda value: value["project_key"].casefold())
        elif project:
            entry = self._entry_by_key(registry, project)
            if not entry:
                raise MemoryError("project key is not enrolled", code="project_not_enrolled", category="configuration", phase="migrate")
            entries = [entry]
        else:
            raise MemoryError("project or all_projects is required", code="project_missing", category="configuration", phase="migrate")
        migrated = []
        for entry in entries:
            source = self._db_path(entry)
            previous_version = self._stored_content_schema_version(entry)
            backup = self._raw_backup(entry, "pre-schema2") if previous_version < MEMORY_SCHEMA_VERSION and source.exists() else None
            connection = self._connect(entry, backup_before_migration=False)
            version = int(connection.execute("SELECT value FROM memory_metadata WHERE key='schema_version'").fetchone()[0])
            connection.close()
            migrated.append({"project": entry["project_key"], "previous_schema_version": previous_version, "schema_version": version, "changed": previous_version != version, "backup": str(backup) if backup else None})
        metrics_schema_version = None
        if self.metrics.path.exists():
            metrics_connection = self.metrics._write_connection()
            metrics_schema_version = int(
                metrics_connection.execute(
                    "SELECT value FROM metrics_metadata WHERE key='schema_version'"
                ).fetchone()[0]
            )
            self.metrics.close()
        return {
            "registry_schema_version": REGISTRY_SCHEMA_VERSION,
            "registry_backup": str(registry_backup) if registry_backup else None,
            "memory_schema_version": MEMORY_SCHEMA_VERSION,
            "metrics_schema_version": metrics_schema_version,
            "metrics_backup": self.metrics.last_migration_backup,
            "projects": migrated,
        }


PROJECT_SELECTOR_PROPERTIES: dict[str, Any] = {
    "project": {
        "type": "string",
        "description": "Hierarchical project key from active workspace instructions, for example ExampleSuite/c-port.",
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
        "name": "project_memory_recall",
        "title": "Recall Project Memory",
        "description": "Adaptively search the selected project and its parent, returning a complete direct action when safe or bounded cards when expansion is needed.",
        "inputSchema": tool_input(
            {
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["compact", "balanced", "deep", "auto"], "default": "auto"},
                "target_tokens": {"type": "integer", "minimum": 128, "default": DEFAULT_TARGET_TOKENS},
                "max_tokens": {"type": "integer", "minimum": 128, "default": DEFAULT_MAX_TOKENS},
                "target_bytes": {"type": "integer", "minimum": 512},
                "max_bytes": {"type": "integer", "minimum": 512},
                "include_parent": {"type": "boolean", "default": True},
                "include_children": {"type": "boolean", "default": False},
                "include_evidence": {"type": "string", "enum": ["auto", "true", "false"], "default": "auto"},
                "task_type": {"type": "string"},
                "risk": {"type": "string", "default": "normal"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "kinds": {"type": "array", "items": {"type": "string"}},
                "importance": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAXIMUM_CARDS},
                "include_cold": {"type": "boolean", "default": False},
                "dismiss": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "record_id": {"type": "string"},
                            "outcome": {"type": "string", "enum": ["reused", "helpful", "not_applicable", "stale"]},
                        },
                        "required": ["record_id", "outcome"],
                    },
                },
            },
            ["query"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "project_memory_read",
        "title": "Read Project Memory View",
        "description": "Expand one selected record to card, action, evidence, or bounded full history without revealing encrypted test credentials.",
        "inputSchema": tool_input(
            {
                "record_id": {"type": "string"},
                "view": {"type": "string", "enum": ["card", "action", "evidence", "full"], "default": "action"},
                "sections": {"type": "array", "items": {"type": "string"}},
                "max_tokens": {"type": "integer", "minimum": 128, "default": DEFAULT_MAX_TOKENS},
                "max_bytes": {"type": "integer", "minimum": 512},
            },
            ["record_id"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "project_memory_remember",
        "title": "Remember Structured Project Knowledge",
        "description": "Record an occurrence, verified solution, constraint, decision, failure pattern, environment fact, checkpoint, relation, or feedback event.",
        "inputSchema": tool_input(
            {
                "operation": {"type": "string", "enum": ["occurrence", "upsert", "finalize", "checkpoint", "failed_attempt", "feedback"]},
                "kind": {"type": "string"},
                "record_id": {"type": "string"},
                "candidate_id": {"type": "string"},
                "stable_key": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "problem": {"type": "string"},
                "context": {"type": "string"},
                "action": {"type": ["string", "array"]},
                "steps": {"type": "array", "items": {"type": "string"}},
                "applicability": {"type": ["string", "object"]},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "verification": {"type": ["string", "array"]},
                "outcome": {"type": "string"},
                "observation": {"type": "string"},
                "statement": {"type": "string"},
                "reason": {"type": "string"},
                "scope": {"type": "string"},
                "severity": {"type": "string"},
                "conditions": {"type": ["string", "array", "object"]},
                "decision": {"type": "string"},
                "alternatives_considered": {"type": "array", "items": {"type": "string"}},
                "rejected_reasons": {"type": "array", "items": {"type": "string"}},
                "consequences": {"type": "array", "items": {"type": "string"}},
                "revisit_when": {"type": "array", "items": {"type": "string"}},
                "symptom": {"type": "string"},
                "attempt": {"type": "string"},
                "observed_result": {"type": "string"},
                "why_it_failed": {"type": "string"},
                "do_not_repeat": {"type": "string"},
                "fact": {"type": "string"},
                "source": {"type": "string"},
                "goal": {"type": "string"},
                "completed": {"type": "array", "items": {"type": "string"}},
                "current_state": {"type": "string"},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "blockers": {"type": "array", "items": {"type": "string"}},
                "branch": {"type": "string"},
                "head_commit": {"type": "string"},
                "versions": {"type": "array", "items": {"type": "string"}},
                "devices": {"type": "array", "items": {"type": "string"}},
                "platforms": {"type": "array", "items": {"type": "string"}},
                "importance": {"type": "string"},
                "confidence": {"type": "string"},
                "verified": {"type": "boolean"},
                "verified_at": {"type": "string"},
                "verified_commit": {"type": "string"},
                "watch_paths": {"type": "array", "items": {"type": "string"}},
                "expires_at": {"type": "string"},
                "supersedes_id": {"type": "string"},
                "relations": {"type": "array", "items": {"type": "object"}},
                "provenance": {"type": "object"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "outcome_feedback": {"type": "string"},
            },
            ["operation"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "project_memory_report_stale",
        "title": "Report Stale Project Memory",
        "description": "Mark a record possibly stale or stale while preserving it, its evidence, and its audit history.",
        "inputSchema": tool_input(
            {
                "record_id": {"type": "string"},
                "reason": {"type": "string"},
                "state": {"type": "string", "enum": ["possibly_stale", "stale"], "default": "stale"},
            },
            ["record_id", "reason"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
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
        "name": "project_memory_stage_test_asset_for_longrun",
        "title": "Stage Test Credential For Longrun",
        "description": (
            "Decrypt one scalar field from an explicitly test-only asset and stage it locally as a "
            "short-lived one-time Longrun stdin handle. The secret value is never returned."
        ),
        "inputSchema": tool_input(
            {
                "record_id": {"type": "string"},
                "secret_field": {"type": "string"},
                "append_newline": {"type": "boolean", "default": True},
            },
            ["record_id", "secret_field"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
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

LEAN_TOOL_NAMES = {
    "project_memory_recall",
    "project_memory_read",
    "project_memory_remember",
    "project_memory_report_stale",
}
COMPAT_TOOL_NAMES = LEAN_TOOL_NAMES | {
    "project_memory_status",
    "project_memory_search",
    "project_memory_get",
    "project_memory_note_repetition",
    "project_memory_finalize_solution",
    "project_memory_record_log_location",
    "project_memory_mark_used",
}


def exposed_tools(memory: ProjectMemory) -> list[dict[str, Any]]:
    if memory.profile == "lean":
        allowed = LEAN_TOOL_NAMES
    elif memory.profile == "compat":
        allowed = COMPAT_TOOL_NAMES
    else:
        allowed = {tool["name"] for tool in TOOLS}
    selected = [copy.deepcopy(tool) for tool in TOOLS if tool["name"] in allowed]
    if memory.bound_project:
        for tool in selected:
            schema = tool["inputSchema"]
            schema["properties"].pop("project", None)
            schema["properties"].pop("project_root", None)
            schema.pop("anyOf", None)
    return selected


TOOL_OPERATIONS = {
    "project_memory_recall": "recall",
    "project_memory_read": "read",
    "project_memory_remember": "write",
    "project_memory_report_stale": "stale",
    "project_memory_status": "probe",
    "project_memory_search": "read",
    "project_memory_get": "read",
    "project_memory_get_test_asset": "sensitive_read",
    "project_memory_stage_test_asset_for_longrun": "sensitive_read",
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
        if memory.bound_project:
            entry = memory._entry_by_key(registry, memory.bound_project)
            return entry["project_id"] if entry else None
        project = args.get("project")
        if isinstance(project, str) and project.strip():
            entry = memory._entry_by_key(registry, project)
            return entry["project_id"] if entry else None
        project_root = args.get("project_root")
        if isinstance(project_root, str) and project_root.strip():
            entry = memory._resolve_root_in_registry(registry, project_root)
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
    if name == "project_memory_remember":
        return "create" if int(result.get("repetition_count", 0)) <= 1 else "edit"
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
        "project_memory_recall": memory.recall,
        "project_memory_read": memory.read,
        "project_memory_remember": memory.remember,
        "project_memory_report_stale": memory.report_stale,
        "project_memory_status": memory.status,
        "project_memory_search": memory.search,
        "project_memory_get": memory.get,
        "project_memory_get_test_asset": lambda value: memory.get(value, include_test_secrets=True),
        "project_memory_stage_test_asset_for_longrun": memory.stage_test_asset_for_longrun,
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
        exposed_names = {tool["name"] for tool in exposed_tools(memory)}
        if name not in handlers or name not in exposed_names:
            raise MemoryError(
                f"unknown tool: {name}",
                code="unknown_tool",
                category="protocol",
                phase="dispatch",
            )
        result = handlers[name](args)
        if name != "project_memory_stats":
            if name == "project_memory_search":
                result_items = len(result.get("results", []))
            elif name == "project_memory_recall":
                result_items = int(result.get("direct_action") is not None) + len(result.get("cards", [])) + len(result.get("alternatives", []))
            else:
                result_items = 0
            budget = result.get("budget", {}) if isinstance(result.get("budget"), dict) else {}
            response_details = {
                **budget,
                "cards_returned": len(result.get("cards", [])) + len(result.get("alternatives", [])),
                "action_records_returned": int(result.get("direct_action") is not None),
                "evidence_records_returned": sum(
                    1 for item in (result.get("direct_action") or {}).get("support", [])
                    if item
                ),
                "full_read": name == "project_memory_read" and str(args.get("view", "action")).casefold() == "full",
                "direct_action": result.get("direct_action") is not None,
                "parent_search": len(result.get("searched_projects", [])) > 1,
            }
            record_metric_safely(
                memory,
                project_id=project_id,
                tool=metric_tool,
                operation=successful_operation(name, result),
                success=True,
                duration_us=(time.monotonic_ns() - started) // 1000,
                result_items=result_items,
                request_argument_bytes=serialized_bytes(args),
                response_bytes=serialized_bytes(result),
                response_details=response_details,
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


def serve(memory: ProjectMemory | None = None) -> None:
    memory = memory or ProjectMemory()
    if memory.bound_project:
        memory.resolve_project_key(memory.bound_project)
    for line in sys.stdin:
        request_id = None
        try:
            message = json.loads(line)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                scope = f" Bound project: {memory.bound_project}." if memory.bound_project else " Pass the mapped hierarchical project key on every call."
                send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "instructions": f"Use adaptive recall first, expand evidence only when coverage requires it, and remember only observed or verified knowledge. For a reviewed Longrun command, stage an existing encrypted test-asset field with project_memory_stage_test_asset_for_longrun and pass only its one-time handle; do not reveal the plaintext or ask the user to re-enter an enrolled credential.{scope} Active tool profile: {memory.profile}."}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": exposed_tools(memory)}})
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
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--project", help="Bind this server process to one enrolled project key")
    serve_parser.add_argument("--profile", choices=sorted(PROFILE_VALUES), help="Tool profile; defaults to PROJECT_MEMORY_PROFILE or admin")
    enroll_parser = subparsers.add_parser("enroll")
    enroll_parser.add_argument("--project-root", help="Project root, or parent root when --subproject is used; defaults to the current directory")
    enroll_parser.add_argument("--project-name", help="Display name; defaults to the project directory name")
    enroll_parser.add_argument("--project-key", help="Stable selector; a new explicit key creates a separate memory even when the root is already enrolled")
    enroll_parser.add_argument("--parent-root", help="Existing enrolled parent for the project root")
    enroll_parser.add_argument("--parent-project", help="Existing parent project key; supports logical subprojects at the same root")
    enroll_parser.add_argument("--subproject", help="Child path relative to the parent project root")
    enroll_parser.add_argument("--allow-test-secrets", action="store_true", default=None)
    enroll_parser.add_argument("-y", "--yes", action="store_true", help="Skip interactive confirmation")
    backup_parser = subparsers.add_parser("backup")
    backup_selector = backup_parser.add_mutually_exclusive_group(required=True)
    backup_selector.add_argument("--project-root")
    backup_selector.add_argument("--project")
    migrate_parser = subparsers.add_parser("migrate", help="Back up and migrate enrolled project databases")
    migrate_selector = migrate_parser.add_mutually_exclusive_group(required=True)
    migrate_selector.add_argument("--project")
    migrate_selector.add_argument("--all", action="store_true", dest="all_projects")
    args = parser.parse_args()
    memory = ProjectMemory(
        bound_project=getattr(args, "project", None) if args.command == "serve" else None,
        profile=getattr(args, "profile", None) if args.command == "serve" else None,
    )
    if args.command == "serve":
        serve(memory)
    elif args.command == "enroll":
        if args.parent_root and args.parent_project:
            parser.error("--parent-root and --parent-project cannot be used together")
        if args.subproject and args.parent_root:
            parser.error("--subproject cannot be combined with --parent-root")
        if args.subproject:
            path_parent_root = args.project_root or os.getcwd()
            child = Path(args.subproject).expanduser()
            project_root = str(child if child.is_absolute() else Path(path_parent_root) / child)
            parent_root = None if args.parent_project else path_parent_root
        else:
            project_root = args.project_root or os.getcwd()
            parent_root = args.parent_root
        preview = memory.preview_enrollment(
            project_root,
            args.project_name,
            args.allow_test_secrets,
            parent_root,
            args.project_key,
            args.parent_project,
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
            args.parent_project,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "backup":
        print(memory.backup(args.project_root, args.project))
    elif args.command == "migrate":
        print(json.dumps(memory.migrate_databases(args.project, args.all_projects), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
