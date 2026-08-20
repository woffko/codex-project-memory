# Security model

Project Memory is a local tool. It does not provide a remote service and does
not send stored records anywhere by itself.

## Storage boundaries

- Databases: `${XDG_DATA_HOME:-~/.local/share}/codex-project-memory/projects/`
- Usage metrics: `${XDG_DATA_HOME:-~/.local/share}/codex-project-memory/usage.sqlite3`
- Registry: `${XDG_DATA_HOME:-~/.local/share}/codex-project-memory/registry.json`
- Encryption key: `${XDG_CONFIG_HOME:-~/.config}/codex-project-memory/master.key`
- Directories are created with mode `0700`; databases, backups, the registry,
  and the key use mode `0600` on POSIX systems.
- Only canonical project roots explicitly enrolled in the registry are
  accepted. Same-named checkouts do not automatically share a database.
- Hierarchical project keys are selectors, not passwords. Keys are unique
  case-insensitively and map to logical projects. Several logical projects may
  intentionally share one canonical root, but each has a separate project ID
  and database. A root-only selector resolves its designated default project.
- A child project's root must equal or be physically contained by its enrolled
  parent root. Parent/meta-project status is derived from registered child
  relationships; adding a child does not move or rewrite the parent's existing
  database.

## Project routing boundary

Active workspace instructions map repository paths and, where needed, task or
component scopes to enrolled project keys and help prevent accidental
cross-project memory use. Portable mappings may
remain in a tracked `AGENTS.md`; machine-specific mappings belong in an
ignored local `AGENTS.override.md`. The installer never creates or modifies
either file. Routing guidance is a workflow boundary, not an authorization
mechanism. A process running as the same OS account and connected to the MCP
server can request another known enrolled project key. Use separate OS
accounts or separate `PROJECT_MEMORY_HOME` instances when projects require a
hard access boundary from each other.

Exclude `AGENTS.override.md` through each target repository's local
`.git/info/exclude`, and verify that it is not already tracked before adding
local paths or keys. At one directory level the override replaces, rather than
extends, `AGENTS.md`, so it must preserve all guidance required from that
directory. Do not store credentials in any Codex instruction file.

Child sessions should search their selected child memory and declared parent
memory separately. Sibling memory is not implicitly searched or exposed.
Same-root logical projects are an organizational routing boundary, not an
access-control boundary; use separate OS accounts or storage homes when hard
isolation is required.

Project-bound mode (`serve --project KEY` or `PROJECT_MEMORY_PROJECT=KEY`)
narrows one server process to a single enrolled key, removes selectors from its
tool schemas, and rejects attempts to target another key or root. It reduces
routing mistakes but is not an OS-level boundary.

## Materialized views and history

`card_json`, `action_json`, and `evidence_json` are deterministic projections
of ordinary structured fields. They never contain `secret_blob` data. View
generation runs the same credential-pattern rejection used by ordinary writes;
a failing view update rolls back the record transaction.

Occurrence, failed-attempt, verification, feedback, supersedence, staleness,
expiry, checkpoint, and migration events remain in each project's private
database. Normal recall excludes cold history, revisions, and audit details.
Full reads remain local and still cannot reveal encrypted test-asset fields.
Provenance must be bounded metadata, never raw logs, full command output,
credentials, personal data, or arbitrary repository contents.

Git-aware freshness stores only explicitly supplied relative watch paths,
working-tree blob hashes, and an optional commit identity. Secret-like paths
such as `.env`, private-key names, and key containers are rejected. Derived
repository context and optional local semantic indexing are not enabled in the
current default implementation; if added later, they must remain rebuildable,
local, opt-in, and secret-excluding.

## Local usage diagnostics

Usage collection is local and enabled by default. Set
`PROJECT_MEMORY_METRICS=0` in the MCP server environment to disable new
collection. The project content databases and the central usage database are
separate, and the dedicated report executable opens only the registry and
usage database in read-only mode.

Daily aggregates retain the stable project ID, tool name, operation class,
success flag, counts, search result counts, duration totals, first/last
timestamps, request/response byte totals, estimated response-token totals,
view counts, recall/read counts, direct-action counts, and budget/parent-search
counts. They do not retain project paths or keys, tool arguments, search
queries, record IDs, record contents, Git file names, exception messages, logs,
or credentials.

Sanitized error events use a 90-day retention window and expired events are
pruned when the next error is recorded. They contain an error ID, project ID
when resolution succeeded, tool and operation, category, phase, structured
error code, exception type, optional machine-readable SQLite or OS code,
server version, and a fingerprint derived from those fields. An error response
may include the error ID so it can be correlated with the local report.
Unknown project selectors are not stored.

`usage.sqlite3` is mode `0600` and may still reveal which enrolled project IDs
are active and when they were used. Treat it as private local operational
metadata. No telemetry is transmitted by this project. Any future export must
be separately implemented, explicit opt-in, and exclude project identifiers.

## Secrets

Ordinary candidates, solutions, and log-location records reject common
credential patterns. Credentials are accepted only by the dedicated
`project_memory_store_test_asset` tool when both conditions hold:

1. the project was enrolled with `--allow-test-secrets`; and
2. the call explicitly sets `test_only: true`.

Secret fields are encrypted with AES-256-GCM and are omitted from FTS indexes,
ordinary and semantic indexes, materialized views, events, relations, metrics,
checkpoints, provenance, derived context, exception messages, revisions, and
normal record reads. Decryption uses a separate tool so Codex can apply an
explicit approval rule.

`project_memory_stage_test_asset_for_longrun` is a non-revealing alternative
for reviewed test-only automation. It decrypts one scalar field in-process,
stages it as a random `0600` file inside a same-user `0700` Longrun secret
directory, and returns only a one-time handle. Neither the secret value nor the
handle is written to Project Memory audit records or aggregate metrics. If the
audit transaction fails, the staged file is removed. Longrun is responsible
for owner/mode/type/TTL validation, immediate unlink, fd delivery, and output
suppression.

This handoff protects Codex transcripts, MCP arguments, argv/environment, and
Longrun logs from the plaintext. It does not protect against root, another
compromised process running as the same OS user, memory inspection, or local
filesystem forensics before the one-time file is consumed. The dedicated
plaintext reveal tool remains available for compatibility and remains
separately approval-gated.

Schema migration never decrypts or rewrites encrypted blobs. Before a schema-1
content database is migrated, the installer creates a consistent mode-`0600`
SQLite backup. Migration is transactional after schema scaffolding and writes
the new version marker only after record conversion and view rebuilding
succeed.

## Benchmark boundary

Passive metrics never infer correctness. The benchmark reads an explicit,
user-selected labeled JSON file and may contain queries and expected record
identity in its process memory and output. Bundled fixtures run in a temporary
storage home. When benchmarking a real project, treat the input and output as
private project artifacts and do not commit them unless they are intentionally
sanitized.

Encryption protects a copied database without its key. It does not protect
against compromise of the same OS account, a malicious process running as the
user, or disclosure after a secret has been intentionally retrieved.

Do not store production, personal, or ambiguously classified credentials.

## Reporting

Do not open a public issue containing a credential, database, key, or private
record. Report security concerns privately to the repository owner through
GitHub Security Advisories when available.
