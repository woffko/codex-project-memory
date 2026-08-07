# Codex Project Memory

Local, project-scoped operational memory for Codex, exposed through MCP. It
helps Codex find previously completed procedures, track recurring attempts,
and retain only a verified final solution.

Each project's data lives outside Git. The plugin does not send records to an
external service and does not place databases, encryption keys, or credentials
in the project repository.

## What it stores

- recurring problems and candidate actions;
- final solutions after at least two occurrences and successful verification;
- stable log locations without copying raw logs;
- explicitly test-only asset details, including encrypted credentials;
- record revisions and a local audit trail.

Search uses SQLite FTS5. Secret fields are encrypted with AES-256-GCM and are
never added to the full-text index.

## Requirements

- Linux, macOS, or WSL;
- Git;
- Python 3.10+ with the `venv` module;
- a recent Codex CLI with the `codex plugin` commands.

## Install from scratch

```bash
git clone https://github.com/woffko/codex-project-memory.git
cd codex-project-memory
chmod +x scripts/install.sh plugins/project-memory/scripts/run-project-memory.sh
./scripts/install.sh
```

The installer creates an isolated Python runtime at
`${XDG_DATA_HOME:-~/.local/share}/codex-project-memory/runtime`, installs the
`cryptography` dependency, registers the cloned repository as a local Codex
marketplace, and installs the `project-memory` plugin.

To register the marketplace directly from GitHub instead of keeping a local
checkout:

```bash
codex plugin marketplace add woffko/codex-project-memory --ref main
codex plugin add project-memory@codex-project-memory
```

The direct GitHub method still requires Python with the `cryptography` package.
The most reproducible setup is to run `scripts/install.sh` once from a clone.

## Enroll a project

Change to the root of the project that should receive its own memory:

```bash
cd /path/to/project
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-root "$PWD" \
  --project-name "my-project"
```

A normal enrollment does not allow credential storage. Enable that capability
separately only when the project works with resources explicitly classified as
test-only:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-root "$PWD" \
  --project-name "my-project" \
  --allow-test-secrets
```

This flag does not mean “allow every secret.” It applies only to `test_asset`
records with an explicit `test_only: true`. Do not store production, personal,
or ambiguously classified credentials.

## Configure Codex in the project

Add the tables from
[`examples/project-config.toml`](examples/project-config.toml) to the trusted
project's `.codex/config.toml`. They keep reads and ordinary memory writes
automatic, while requiring approval to store or reveal test-only credentials
and to deprecate a record.

Add the guidance from [`examples/AGENTS.md`](examples/AGENTS.md) to the
project's `AGENTS.md`. The plugin already bundles the same workflow as a skill,
but project guidance makes the intended behavior explicit and durable.

After installation or configuration changes, start a new Codex session from
the enrolled project root:

```bash
codex -C /path/to/project
```

When resuming a session, choose the enrolled project directory if Codex asks
which working directory to use.

## How a recurring action becomes a solution

1. Codex calls `project_memory_status` and `project_memory_search` before
   troubleshooting.
2. A recurring problem or action is recorded with
   `project_memory_note_repetition`.
3. The server increments the matching candidate using a stable fingerprint.
4. A candidate cannot become a solution before two occurrences are recorded.
5. After a real successful check, Codex calls
   `project_memory_finalize_solution` with the exact final steps, outcome, and
   verification evidence.

Untested hypotheses and raw logs should never become final solutions.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `project_memory_status` | Confirm enrollment and show record counts |
| `project_memory_search` | Search solutions and metadata without secrets |
| `project_memory_get` | Read a normal record |
| `project_memory_note_repetition` | Record another occurrence of a problem/action |
| `project_memory_finalize_solution` | Save the verified final variant |
| `project_memory_record_log_location` | Remember a stable log location |
| `project_memory_store_test_asset` | Store a test-only asset and encrypted fields |
| `project_memory_get_test_asset` | Reveal encrypted fields with approval |
| `project_memory_deprecate` | Soft-deprecate an obsolete record |

## Connect only the MCP server

The plugin is preferred because it installs the workflow skill together with
the server. To register only the MCP server:

```bash
codex mcp add project_memory -- \
  ~/.local/share/codex-project-memory/runtime/bin/codex-project-memory serve
```

The project must still be registered with the `enroll` command.

## Storage and backups

Default layout:

```text
~/.local/share/codex-project-memory/
├── registry.json
├── runtime/
└── projects/<project-id>/
    ├── memory.sqlite3
    └── backups/

~/.config/codex-project-memory/
└── master.key
```

Create a consistent SQLite backup:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory backup \
  --project-root /path/to/project
```

Never copy `master.key` into a repository. Restoring encrypted test fields
requires both the database and its corresponding key.

See [`SECURITY.md`](SECURITY.md) for the full security boundary.

## Update

For an installation made from a local clone:

```bash
git pull --ff-only
./scripts/install.sh
```

For a GitHub marketplace installation:

```bash
codex plugin marketplace upgrade codex-project-memory
codex plugin add project-memory@codex-project-memory
```

Start a new Codex thread after updating so the refreshed skill and MCP tool
definitions are loaded.

## Development checks

```bash
python3 plugins/project-memory/scripts/test_project_memory.py
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/project-memory
```

The tests cover the minimum occurrence count, verified finalization, project
isolation, rejection of credentials in normal records, XDG storage paths, and
the absence of plaintext test credentials in SQLite.

## Uninstall

```bash
./scripts/uninstall.sh
```

Uninstalling the plugin intentionally preserves local databases and backups.

## License

MIT. See [`LICENSE`](LICENSE).
