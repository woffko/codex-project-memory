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
