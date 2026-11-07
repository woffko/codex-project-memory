#!/bin/sh
# Claude Code SessionStart hook: add Project Memory routing for the session
# working directory. It reads only the enrollment registry and must never
# break session start, so it always exits successfully.

data_root="${PROJECT_MEMORY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-project-memory}"
runtime_command="$data_root/runtime/bin/codex-project-memory"

if [ -x "$runtime_command" ]; then
    "$runtime_command" route --hook claude-session-start || true
    exit 0
fi

if python3 -c 'import cryptography' >/dev/null 2>&1; then
    script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    python3 "$script_dir/project_memory_mcp.py" route --hook claude-session-start || true
    exit 0
fi

echo "project-memory: runtime missing; run the repository install script" >&2
exit 0
