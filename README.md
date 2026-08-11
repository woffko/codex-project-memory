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
- record revisions and a local audit trail;
- local usage aggregates and sanitized error diagnostics.

Search uses SQLite FTS5. Secret fields are encrypted with AES-256-GCM and are
never added to the full-text index. Usage metrics never contain queries, tool
arguments, project paths, record contents, record IDs, or credentials.

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
marketplace, and installs the `project-memory` plugin. It does not create,
replace, or modify any `AGENTS.md` or `AGENTS.override.md` file. Project routing
remains an explicit, separate configuration step.

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
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll
```

`--project-root` defaults to the current directory. `--project-name` and the
stable project key both default to its folder name, while either can be
overridden independently. Interactive terminals show the resolved root, name,
and key before enrollment; pass `--yes` to skip confirmation. Explicit values
remain available:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-root "$PWD" \
  --project-name "My Project" \
  --project-key "my-project" \
  --yes
```

The project key is the stable selector used in MCP calls. It is not a password
or security token. The canonical enrolled root remains the storage identity
and the containment boundary used when attaching subprojects.

### Enroll related subprojects

An ordinary project automatically becomes a meta-project when its first child
is enrolled. Its existing database becomes shared parent memory without being
moved or rewritten. From an enrolled parent root:

```bash
cd /path/to/ExampleSuite

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --subproject main

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --subproject c-port

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --subproject rust-port
```

This derives the hierarchical keys `ExampleSuite/main`,
`ExampleSuite/c-port`, and `ExampleSuite/rust-port`. Use `--project-name` for
a friendlier display name and `--project-key` when a different stable selector
is preferable. The equivalent form from a child directory is:

```bash
cd /path/to/ExampleSuite/c-port
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --parent-root ..
```

Subprojects must be inside their enrolled parent root. Parent status reports
its children and `is_meta_project: true`; child status reports its
`parent_project`. Searches remain exact: local Codex guidance directs Codex to
search the active child and its parent separately.

A normal enrollment does not allow credential storage. Enable that capability
separately only when the project works with resources explicitly classified as
test-only:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
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

Project Memory routing belongs in the active workspace instructions; plugin
installation does not change those files. Keep a portable mapping in an
existing tracked `AGENTS.md` when that is appropriate for every checkout. If
the mapping contains machine-specific paths or project keys, use an ignored
local `AGENTS.override.md` instead.

**Important:** at each directory level, Codex loads at most one instruction
file. `AGENTS.override.md` takes precedence over and replaces `AGENTS.md` in
the same directory; the two files are not merged. Before enabling an override,
copy every same-directory instruction that must remain active into it. Codex
does concatenate the selected instruction files from parent directories down
to the working directory, so a nested override can specialize a child while a
selected parent instruction file remains in the chain.

First, exclude that filename in the target repository without changing its
shared `.gitignore`:

```gitignore
# .git/info/exclude
AGENTS.override.md
```

A pattern without a slash excludes that filename at any depth in the current
repository. Independent nested repositories have their own `.git` directory
and need the same local exclusion separately. Exclusion does not make an
already tracked file private, so confirm the destination is ignored and
untracked before adding local paths or keys:

```bash
git check-ignore -v AGENTS.override.md
git ls-files --error-unmatch AGENTS.override.md
```

The first command should identify `.git/info/exclude`; the second should fail
because the file is not tracked. Then copy and customize
[`examples/AGENTS.override.md`](examples/AGENTS.override.md) in the workspace
or relevant subproject. Keep the root override aware of every child path when
sessions start from a common meta-project root. A closer nested override can
supply a child-specific mapping when Codex starts inside that directory.

The plugin bundles the generic memory workflow as a skill, while the ignored
override supplies the exact local mapping. Do not put credentials in the
override. Codex builds its instruction chain when a session starts, so begin a
new session after creating or changing the file.

Existing mappings in `AGENTS.md` remain compatible when no same-directory
`AGENTS.override.md` replaces that file. Installing or updating Project Memory
does not require moving an existing mapping into an override.

After installation or configuration changes, start a new Codex session from
the enrolled project or common meta-project root:

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
6. When a retrieved record is applied or found unsuitable, Codex calls
   `project_memory_mark_used` with `reused`, `helpful`, `not_applicable`, or
   `stale` so usage reports distinguish retrieval from actual reuse.

Untested hypotheses and raw logs should never become final solutions.

## Local usage and error reports

Every project-scoped MCP call updates local daily aggregates in
`usage.sqlite3`. Probes, reads, creates, edits, reuse feedback, search hits,
errors, active days, and approximate MCP server runs remain separate. A server
run is not an exact Codex thread count because the MCP protocol does not supply
a stable Codex session identifier. Successful `project_memory_stats` calls are
not counted, avoiding an observer effect in the reported totals.

The dedicated reporter opens metrics in SQLite read-only mode and never opens
project content databases or decrypts records:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory-report \
  summary --all --since 30d

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory-report \
  summary --project ExampleSuite --include-children --since 90d

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory-report \
  errors --project ExampleSuite/c-port --since 30d

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory-report \
  errors --all --last 20 --error-code sqlite_busy
```

Both commands support `--format table`, `--format json`, and `--format csv`.
Individual sanitized errors use a 90-day retention window; expired events are
pruned when the next error is recorded. They contain a generated error ID,
project ID, tool, operation, category, phase, structured error code, exception
type, machine-readable SQLite or OS code when available, server version, and a
stable fingerprint. Raw exception messages and user values are not retained.

Set `PROJECT_MEMORY_METRICS=0` in the MCP server environment to disable new
collection. Existing metrics remain available to the read-only reporter until
removed manually.

## MCP tools

Pass the hierarchical `project` key from the active workspace instruction
mapping on every new tool call. Codex supplies this selector when it invokes
the MCP tool; the user does not need to type it for every call. Explicit
selection is required because one global MCP server may serve several enrolled
project databases and must not guess the read or write target. A local
`AGENTS.override.md` is optional and is only needed for machine-specific
routing. Legacy `project_root` arguments remain supported for existing
configurations. Project keys select memory but are not authentication
credentials.

| Tool | Purpose |
| --- | --- |
| `project_memory_status` | Confirm enrollment and show counts plus parent/child routing |
| `project_memory_search` | Search one selected project's records without secrets |
| `project_memory_get` | Read a normal record from one selected project |
| `project_memory_stats` | Read local usage aggregates and sanitized error groups |
| `project_memory_note_repetition` | Record another occurrence of a problem/action |
| `project_memory_finalize_solution` | Save the verified final variant |
| `project_memory_record_log_location` | Remember a stable log location |
| `project_memory_store_test_asset` | Store a test-only asset and encrypted fields |
| `project_memory_get_test_asset` | Reveal encrypted fields with approval |
| `project_memory_mark_used` | Mark a retrieved record as reused, helpful, unsuitable, or stale |
| `project_memory_deprecate` | Soft-deprecate an obsolete record |

## Connect only the MCP server

The plugin is preferred because it installs the workflow skill together with
the server. To register only the MCP server:

```bash
codex mcp add project_memory -- \
  ~/.local/share/codex-project-memory/runtime/bin/codex-project-memory serve
```

Projects and subprojects must still be registered with the `enroll` command.

## Storage and backups

Default layout:

```text
~/.local/share/codex-project-memory/
├── registry.json
├── usage.sqlite3
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
  --project ExampleSuite/c-port
```

`backup --project-root /path/to/project` remains available for legacy scripts.

### Registry migration

Registry schema 1 entries are upgraded to schema 2 when read and persisted on
the next enrollment. Existing project IDs, SQLite directories, encrypted
records, and backups are preserved. Existing projects receive a project key
from their stored project name; duplicate legacy names receive a stable hash
suffix and should be added to the workspace's local `AGENTS.override.md`
routing explicitly.

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
python3 plugins/project-memory/scripts/test_project_memory_metrics.py
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/project-memory
```

The tracked plugin manifest uses a canonical SemVer version. Ordinary feature
branches must not commit timestamp cachebuster suffixes. Bump the repository
version once in an integration or release change; local development installs
may add a cachebuster only in an untracked installation or staging copy.

The tests cover hierarchical project routing, legacy registry migration,
stable encrypted-record identity, minimum occurrence counts, verified
finalization, project isolation, credential rejection, XDG storage paths,
operation classification, hierarchy reports, read-only reporting, structured
errors, and the absence of plaintext credentials and project paths in metrics.

## Uninstall

```bash
./scripts/uninstall.sh
```

Uninstalling the plugin intentionally preserves local databases, usage
metrics, sanitized errors, and backups.

## License

MIT. See [`LICENSE`](LICENSE).
