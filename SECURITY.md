# Security model

Project Memory is a local tool. It does not provide a remote service and does
not send stored records anywhere by itself.

## Storage boundaries

- Databases: `${XDG_DATA_HOME:-~/.local/share}/codex-project-memory/projects/`
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

The workspace's `AGENTS.md` maps repository paths to enrolled project keys and
prevents accidental cross-project memory use. That instruction is a workflow
boundary, not an authorization mechanism. A process running as the same OS
account and connected to the MCP server can request another known enrolled
project key. Use separate OS accounts or separate `PROJECT_MEMORY_HOME`
instances when projects require a hard access boundary from each other.

Child sessions should search their selected child memory and declared parent
memory separately. Sibling memory is not implicitly searched or exposed.

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
