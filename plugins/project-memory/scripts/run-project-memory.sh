#!/bin/sh
set -eu

data_root="${PROJECT_MEMORY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-project-memory}"
runtime_command="$data_root/runtime/bin/codex-project-memory"

if [ -x "$runtime_command" ]; then
    exec "$runtime_command" serve
fi

if python3 -c 'import cryptography' >/dev/null 2>&1; then
    script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python3 "$script_dir/project_memory_mcp.py" serve
fi

echo "project-memory: Python dependency 'cryptography' is missing; run the repository install script" >&2
exit 1
