#!/bin/sh
set -eu

data_root="${PROJECT_MEMORY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-project-memory}"

codex plugin remove project-memory@codex-project-memory 2>/dev/null || true
echo "Plugin removed. Local memory was preserved at: $data_root"
echo "Remove that directory manually only if you intentionally want to delete all project memories and backups."
