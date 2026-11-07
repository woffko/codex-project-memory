#!/bin/sh
set -eu

usage() {
    echo "Usage: $0 [--codex] [--claude]"
    echo "Without options, the plugin is registered with every installed CLI (codex, claude)."
}

want_codex=0
want_claude=0
for argument in "$@"; do
    case "$argument" in
        --codex) want_codex=1 ;;
        --claude) want_claude=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if [ "$want_codex" -eq 0 ] && [ "$want_claude" -eq 0 ]; then
    if command -v codex >/dev/null 2>&1; then want_codex=1; fi
    if command -v claude >/dev/null 2>&1; then want_claude=1; fi
    if [ "$want_codex" -eq 0 ] && [ "$want_claude" -eq 0 ]; then
        echo "Codex CLI or Claude Code CLI is required" >&2
        exit 1
    fi
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
plugin_root="$repo_root/plugins/project-memory"
data_root="${PROJECT_MEMORY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-project-memory}"
runtime_root="$data_root/runtime"

command -v python3 >/dev/null 2>&1 || {
    echo "python3 is required" >&2
    exit 1
}
if [ "$want_codex" -eq 1 ]; then
    command -v codex >/dev/null 2>&1 || {
        echo "Codex CLI is required for --codex" >&2
        exit 1
    }
fi
if [ "$want_claude" -eq 1 ]; then
    command -v claude >/dev/null 2>&1 || {
        echo "Claude Code CLI is required for --claude" >&2
        exit 1
    }
fi

python3 -m venv "$runtime_root"
"$runtime_root/bin/python" -m pip install --upgrade pip
"$runtime_root/bin/python" -m pip install --upgrade "$plugin_root"

# Upgrade project content databases only after making per-project SQLite backups.
# This command is repeat-safe and reports unchanged schema-2 databases without
# creating another backup.
"$runtime_root/bin/codex-project-memory" migrate --all

if [ "$want_codex" -eq 1 ]; then
    if ! codex plugin marketplace list 2>/dev/null | grep -q 'codex-project-memory'; then
        codex plugin marketplace add "$repo_root"
    fi
    codex plugin add project-memory@codex-project-memory
fi

if [ "$want_claude" -eq 1 ]; then
    # A local-path marketplace is loaded in place: the plugin reads this
    # checkout, so keep it on a branch that contains .claude-plugin/.
    if ! claude plugin marketplace list 2>/dev/null | grep -q 'codex-project-memory'; then
        claude plugin marketplace add "$repo_root" --scope user
    fi
    claude plugin install project-memory@codex-project-memory --scope user
fi

echo
echo "Project Memory installed. Enroll a project from its root with:"
echo "  $runtime_root/bin/codex-project-memory enroll"
echo "Add a child from an enrolled parent with:"
echo "  $runtime_root/bin/codex-project-memory enroll --subproject RELATIVE-PATH"
echo "Show which project keys apply to a directory with:"
echo "  $runtime_root/bin/codex-project-memory route --path DIR --format context"
echo "View local usage and sanitized errors with:"
echo "  $runtime_root/bin/codex-project-memory-report summary --all --since 30d"
echo "Run the local quality-and-cost benchmark with:"
echo "  $runtime_root/bin/codex-project-memory-bench --queries PATH/TO/recall_cases.json"
echo "The installer did not create or modify AGENTS.md, AGENTS.override.md, CLAUDE.md, or Claude Code permissions."
echo
echo "Add --allow-test-secrets only for projects that may store credentials for explicitly test-only assets."
echo "Start a new Codex thread or Claude Code session after enrollment."
