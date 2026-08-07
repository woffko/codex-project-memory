---
name: project-memory
description: Use local project-scoped MCP memory when troubleshooting, repeating operational actions, working with enrolled test equipment, or locating project logs. Search before repeating work; track recurring attempts; save only a verified final solution.
---

# Project Memory workflow

Use the `project_memory` MCP tools only for the current enrolled project. Pass
the canonical current project root as `project_root` on every call. Do not use a
different checkout merely because it has the same repository name.

## Before troubleshooting or repeating an operational action

1. Call `project_memory_status` to confirm the project is enrolled.
2. Call `project_memory_search` using the task, error signature, device name,
   command, or log purpose.
3. Reuse a stored solution only when its context and verification still match.

## Repetitions and final solutions

- Call `project_memory_note_repetition` when substantially the same problem or
  operational action recurs while an effective final variant is still being
  sought.
- Keep observations concise. Do not copy raw logs into memory; record their
  stable location with `project_memory_record_log_location`.
- A candidate becomes eligible only after at least two occurrences.
- Call `project_memory_finalize_solution` only after the final steps were
  actually executed and the stated verification succeeded.
- Store the exact relevant context, final steps, outcome, verification method,
  versions or device constraints, and useful tags. Do not store guesses or an
  untested proposal as a solution.

## Test equipment

- The user authorizes this memory to retain routes, addresses, usernames,
  credentials, access methods, and log locations for equipment explicitly
  identified as test-only in an enrolled project.
- Store credentials only through `project_memory_store_test_asset`, with
  `test_only: true`, placing passwords, tokens, and keys inside `secret_fields`.
- Keep non-secret routing and location data in `endpoint`, `paths`, and `notes`.
- Use `project_memory_get_test_asset` only when the current task needs the
  credential. Never reproduce a retrieved secret in chat, source files, Git,
  patches, logs, delegated prompts, or unrelated tool arguments.
- Production, personal, or ambiguously classified credentials are not covered
  by this authorization. Do not store them.

## Maintenance

- Use `project_memory_get` for ordinary records; it never reveals encrypted
  fields.
- Deprecate stale records instead of deleting them. Record why the old solution
  is no longer valid.
- If the MCP server is unavailable, say so; do not claim that memory was
  searched or updated.
