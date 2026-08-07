#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
plugin_root="$repo_root/plugins/project-memory"
data_root="${PROJECT_MEMORY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-project-memory}"
runtime_root="$data_root/runtime"

command -v python3 >/dev/null 2>&1 || {
    echo "python3 is required" >&2
    exit 1
}
command -v codex >/dev/null 2>&1 || {
    echo "Codex CLI is required" >&2
    exit 1
}

python3 -m venv "$runtime_root"
"$runtime_root/bin/python" -m pip install --upgrade pip
"$runtime_root/bin/python" -m pip install --upgrade "$plugin_root"

if ! codex plugin marketplace list 2>/dev/null | grep -q 'codex-project-memory'; then
    codex plugin marketplace add "$repo_root"
fi
codex plugin add project-memory@codex-project-memory

echo
echo "Project Memory installed. Enroll a project from its root with:"
echo "  $runtime_root/bin/codex-project-memory enroll"
echo "Add a child from an enrolled parent with:"
echo "  $runtime_root/bin/codex-project-memory enroll --subproject RELATIVE-PATH"
echo
echo "Add --allow-test-secrets only for projects that may store credentials for explicitly test-only assets."
echo "Start a new Codex thread after enrollment."
