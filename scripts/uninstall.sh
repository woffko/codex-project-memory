#!/bin/sh
set -eu

data_root="${PROJECT_MEMORY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-project-memory}"

if command -v codex >/dev/null 2>&1; then
    codex plugin remove project-memory@codex-project-memory 2>/dev/null || true
fi
if command -v claude >/dev/null 2>&1; then
    claude plugin uninstall project-memory@codex-project-memory --scope user 2>/dev/null || true
fi
echo "Plugin removed. Local memory was preserved at: $data_root"
echo "Remove that directory manually only if you intentionally want to delete all project memories and backups."
