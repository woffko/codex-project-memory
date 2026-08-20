# Codex Project Memory

Local, project-scoped operational memory for Codex, exposed through MCP. It
optimizes total context per correctly completed task: useful knowledge remains
losslessly local, while adaptive recall returns only the complete action core
and supporting evidence needed now.

Each project's data lives outside Git. The plugin does not send records to an
external service and does not place databases, encryption keys, or credentials
in the project repository.

## What it stores

- recurring problems and candidate actions;
- verified solutions, constraints, decisions, failure patterns, environment
  facts, and expiring continuation checkpoints;
- materialized `card`, `action`, and `evidence` views plus bounded `full` reads;
- stable log locations without copying raw logs;
- explicitly test-only asset details, including encrypted credentials;
- record revisions and a local audit trail;
- local usage aggregates and sanitized error diagnostics.

Recall uses exact technical signals, weighted SQLite FTS5, relaxed lexical
fallback, project scope, confidence, freshness, conflict state, and serialized
cost. Optional local semantics are not required and no model is downloaded.
Secret fields are encrypted with AES-256-GCM and are never added to ordinary
views or indexes. Usage metrics never contain queries, tool arguments, project
paths, record contents, record IDs, or credentials.

## Adaptive retrieval

`project_memory_recall` is the normal read entry point. It searches the active
project and its declared parent in one call and selects one of four modes:

- `compact`: a complete short action for an exact, safe match;
- `balanced`: the action core plus material constraints or failures;
- `deep`: evidence, rationale, conflicts, and diverse supporting records;
- `auto`: deterministic selection based on match confidence, ambiguity, task
  type, risk, completeness, and staleness.

The preferred budget defaults to 3,000 estimated tokens and the maximum to
8,000. UTF-8 bytes are the enforcement boundary because model tokenizers vary.
Critical applicability, steps, constraints, warnings, verification, and
version or device limits are not removed merely to hit the preferred target.
Coverage and omission metadata state what remains locally available.

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

Installation also runs the repeat-safe content migration for all enrolled
projects. A schema-1 database is backed up as
`projects/<id>/backups/pre-schema2-<timestamp>.sqlite3` before it is changed.
Already migrated databases are left untouched and do not receive another
backup. Existing IDs, hierarchy, encrypted blobs, revisions, audit history,
metrics, and backup directories are preserved.
An existing schema-1 `usage.sqlite3` is likewise backed up under
`backups/usage-pre-schema2-<timestamp>.sqlite3` before aggregate columns are
added.

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
or security token. A root has one default project for backward-compatible
`project_root` calls, but it may also have additional logical projects selected
by key. Each project key has its own stable project ID and SQLite database.

With no explicit `--project-key`, enrollment selects or updates the root's
default project. An explicit key that already exists selects that project. A
new explicit key on an enrolled root creates another independent project; it
does not rename or replace the default project or its records.

### Enroll multiple projects at one repository root

Use this when one repository has distinct work scopes such as a core library
and GUI, but all Codex sessions must start at the same root:

```bash
cd /path/to/Product

# Existing or shared default memory for the repository.
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-name "Product" \
  --project-key "Product"

# Independent memories at the exact same canonical root.
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-name "Product Core" \
  --project-key "Product/core" \
  --parent-project "Product"

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-name "Product GUI" \
  --project-key "Product/gui" \
  --parent-project "Product"
```

`--parent-project` selects the logical parent by project key, so it remains
unambiguous when parent and child use the same path. Omit it if the memories
should be independent peers. Status reports `same_root_projects`,
`is_default_for_root`, `parent_project`, and `children`. Workspace instructions
must route by task or component as well as path because the path alone cannot
distinguish these memories.

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

Nested-directory subprojects must be inside their enrolled parent root. Parent
status reports its children and `is_meta_project: true`; child status reports its
`parent_project`. Searches remain exact: local Codex guidance directs Codex to
search the active child and its parent separately.

If the intended parent is not the default project for its root, select it by
key. The shorthand still derives the child path from `--project-root`:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-root /path/to/Product \
  --subproject plugins/gui \
  --project-key Product/core/gui-plugin \
  --parent-project Product/core
```

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

The default server profile is `admin` for backward compatibility. Set
`PROJECT_MEMORY_PROFILE=lean` to expose only recall, read, remember, and
report-stale, or `compat` to add ordinary legacy tools without administrative
statistics, deprecation, or secret operations. A narrowly scoped server can be
bound to one enrolled key:

```bash
codex-project-memory serve --project Product/core --profile lean
```

Bound mode removes repeated project selectors from tool schemas and rejects
cross-project requests. Multi-project mode remains the installation default.

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

1. Codex calls `project_memory_recall` before repeating troubleshooting.
2. A real occurrence is stored through `project_memory_remember` with a stable
   key and structured action fields.
3. The server increments the candidate and moves the observation into an event,
   outside the hot action payload.
4. A candidate cannot become a solution before two qualifying occurrences.
5. Complete verified evidence can automatically finalize the candidate. The
   action view then contains applicability, steps, constraints, warnings,
   verification, outcome, and version or device limits.
6. Important failed approaches remain available in evidence view; ordinary
   attempts, revisions, and audit history remain cold until explicitly read.
7. Stale knowledge is reported with `project_memory_report_stale`, not deleted.

Untested hypotheses and raw logs should never become final solutions.

Other durable record kinds do not require artificial repetition: constraints,
decisions, failure patterns, and environment facts can be stored once with
their required structured fields and honest confidence. Checkpoints use a TTL
and are returned only for continuation work.

## Local usage and error reports

Every project-scoped MCP call updates local daily aggregates in
`usage.sqlite3`. Probes, reads, creates, edits, reuse feedback, search hits,
errors, active days, and approximate MCP server runs remain separate. Schema 2
also counts request and response bytes, estimated response tokens, returned
cards/actions/evidence, full reads, recall/read round trips, direct actions,
quality-budget expansion, maximum-budget events, and parent searches. These
are aggregate counters only. A server run is not an exact Codex thread count
because MCP does not supply a stable Codex session identifier. Successful
`project_memory_stats` calls are not counted, avoiding an observer effect.

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
project databases, including several at one root, and must not guess the read
or write target. A local
`AGENTS.override.md` is optional and is only needed for machine-specific
routing. Legacy `project_root` arguments remain supported for existing
configurations and resolve only the root's default project. Project keys select
memory but are not authentication credentials.

| Tool | Purpose |
| --- | --- |
| `project_memory_recall` | Parent-aware adaptive retrieval with budgets, quality gates, conflicts, and coverage |
| `project_memory_read` | Expand one record to `card`, `action`, `evidence`, or bounded `full` |
| `project_memory_remember` | Store occurrences, structured knowledge, failures, checkpoints, relations, or feedback |
| `project_memory_report_stale` | Preserve but down-rank knowledge that no longer applies |
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

The first four tools form the `lean` profile. `compat` also exposes ordinary
legacy tools. `admin` exposes every tool, including statistics, deprecation,
and approval-gated test-secret operations. Legacy calls retain their original
arguments and project-root behavior.

Representative MCP arguments:

```json
{"project":"Product/core","query":"E521 after interrupted turn","mode":"auto","risk":"normal"}
{"project":"Product/core","record_id":"7f2a","view":"evidence","max_tokens":8000}
{"project":"Product/core","operation":"checkpoint","stable_key":"retrieval-upgrade","goal":"finish retrieval","completed":["schema"],"current_state":"ranking untested","next_steps":["benchmark"],"blockers":[],"branch":"deepdive","head_commit":"a1b2c3d","expires_at":"2026-09-03T00:00:00Z"}
{"project":"Product/core","record_id":"7f2a","reason":"watched implementation changed","state":"possibly_stale"}
```

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

Migrate one project or every enrolled database explicitly:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory migrate \
  --project Product/core

~/.local/share/codex-project-memory/runtime/bin/codex-project-memory migrate --all
```

### Content schema migration

Content schema 2 adds events, relations, materialized views, completeness,
importance, confidence, provenance, staleness, supersedence, checkpoints, and
Git watch metadata. Migration is transactional and repeat-safe. Legacy
observations and attempt history move into events exactly once, and the old
record revision is saved before its hot payload is rewritten. Missing legacy
fields remain explicitly incomplete; they are never fabricated merely to pass
the direct-action gate. Secret blobs are neither decrypted nor rewritten.

### Registry migration

Registry schema 1 and 2 entries are upgraded to schema 3 when read and
persisted on the next enrollment. Existing project IDs, default-root
resolution, SQLite directories, parent links, encrypted records, metrics, and
backups are preserved. The schema 3 root index allows multiple project IDs to
refer to one canonical root while retaining the old entry as that root's
default. Existing projects without a key receive one from their stored project
name; duplicate legacy names receive a stable hash suffix and should be added
to the workspace's local `AGENTS.override.md` routing explicitly.

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

## Quality and cost benchmark

Run the bundled deterministic fixture across the legacy workflow and every
recall mode:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory-bench \
  --queries plugins/project-memory/testdata/recall_cases.json \
  --compare legacy,compact,balanced,auto,deep
```

The report includes hit@1, hit@3, labeled completion rate, mandatory-field
coverage, direct-action rate, average and p95 response bytes, estimated tokens,
tool calls, and per-case details. Query text and expected record identity exist
only in the explicit benchmark input/output; passive metrics never store them.
Use your own labeled cases with `--project PROJECT_KEY --queries FILE` when the
file has no fixture records. Do not claim token or quality improvements from
response size alone.

See [`BENCHMARK.md`](BENCHMARK.md) for the checked-in v0.5.0 fixture result and
its limitations.

## Development checks

```bash
python3 plugins/project-memory/scripts/test_project_memory.py
python3 plugins/project-memory/scripts/test_project_memory_metrics.py
python3 plugins/project-memory/scripts/test_project_memory_adaptive.py
python3 plugins/project-memory/scripts/test_project_memory_benchmark.py
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/project-memory
```

The tracked plugin manifest uses a canonical SemVer version. Ordinary feature
branches must not commit timestamp cachebuster suffixes. Bump the repository
version once in an integration or release change; local development installs
may add a cachebuster only in an untracked installation or staging copy.

The tests cover hierarchical and same-root project routing, schema 1 and 2
migration, stable encrypted-record identity, default-root compatibility,
minimum occurrence counts, verified finalization, project isolation,
credential rejection, XDG storage paths, per-project operation classification,
hierarchy reports, read-only reporting, structured errors, and the absence of
plaintext credentials and project paths in metrics.

## Uninstall

```bash
./scripts/uninstall.sh
```

Uninstalling the plugin intentionally preserves local databases, usage
metrics, sanitized errors, and backups.

## License

MIT. See [`LICENSE`](LICENSE).
