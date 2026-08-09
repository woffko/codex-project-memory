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
  case-insensitively, map to canonical enrolled roots, and do not replace root
  validation.
- A subproject must be physically contained by its enrolled parent root.
  Parent/meta-project status is derived from registered child relationships;
  adding a child does not move or rewrite the parent's existing database.

## Project routing boundary

An ignored, machine-local `AGENTS.override.md` maps repository paths to
enrolled project keys and helps prevent accidental cross-project memory use.
Do not commit that mapping or copy it into the repository's portable
`AGENTS.md`. The override is a workflow boundary, not an authorization
mechanism. A process running as the same OS account and connected to the MCP
server can request another known enrolled project key. Use separate OS
accounts or separate `PROJECT_MEMORY_HOME` instances when projects require a
hard access boundary from each other.

Exclude `AGENTS.override.md` through each target repository's local
`.git/info/exclude`, and verify that it is not already tracked before adding
local paths or keys. Do not store credentials in any Codex instruction file.

Child sessions should search their selected child memory and declared parent
memory separately. Sibling memory is not implicitly searched or exposed.

## Local usage diagnostics

Usage collection is local and enabled by default. Set
`PROJECT_MEMORY_METRICS=0` in the MCP server environment to disable new
collection. The project content databases and the central usage database are
separate, and the dedicated report executable opens only the registry and
usage database in read-only mode.

Daily aggregates retain the stable project ID, tool name, operation class,
success flag, counts, search result counts, duration totals, and first/last
timestamps. They do not retain project paths or keys, tool arguments, search
queries, record IDs, record contents, exception messages, logs, or credentials.

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
ordinary search results, revisions, and normal record reads. Decryption uses a
separate tool so Codex can apply an explicit approval rule.

Encryption protects a copied database without its key. It does not protect
against compromise of the same OS account, a malicious process running as the
user, or disclosure after a secret has been intentionally retrieved.

Do not store production, personal, or ambiguously classified credentials.

## Reporting

Do not open a public issue containing a credential, database, key, or private
record. Report security concerns privately to the repository owner through
GitHub Security Advisories when available.
