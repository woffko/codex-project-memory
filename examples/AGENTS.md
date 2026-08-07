# Project Memory

## Project routing

Replace this example with the exact enrolled project keys and repository paths
for this workspace.

- Shared/meta-project memory: `OpenMeta`.
- Files under `OpenMeta/` use project `OpenMeta/OpenMeta`.
- Files under `OpenMeta-c/` use project `OpenMeta/OpenMeta-c`.
- Files under `OpenMeta-rc/` use project `OpenMeta/OpenMeta-rc`.
- Files and procedures shared by the whole workspace use project `OpenMeta`;
  add an explicit child mapping for any other repository that needs separate
  memory.
- At task start, determine the active project from the task and target paths.
- Search the active child project first, then search its declared parent shown
  by `project_memory_status` for shared procedures, test equipment, and
  infrastructure.
- Write component-specific memory to the active child. Write genuinely shared
  memory explicitly to the parent.
- Do not search sibling projects unless the task spans them. If routing is
  ambiguous, ask before writing memory.

## Memory workflow

- At the start of troubleshooting or repeated operational work, call
  `project_memory_status` with the mapped `project` key, then search for the
  task, error signature, command, device, or log purpose.
- After evaluating a retrieved record, call `project_memory_mark_used` with
  `reused`, `helpful`, `not_applicable`, or `stale`; a search hit alone does not
  mean the memory was used.
- Call `project_memory_note_repetition` when substantially the same problem or
  action recurs while an effective final variant is still being sought.
- Finalize a candidate only after at least two recorded occurrences and
  successful real verification. Save the exact final steps, context, outcome,
  verification, constraints, and tags.
- Do not copy raw logs into memory. Store stable log locations with
  `project_memory_record_log_location`.
- Store credentials only for assets explicitly classified as test-only,
  through `project_memory_store_test_asset` with `test_only = true` and secret
  values in `secret_fields`.
- Never reproduce retrieved credentials in chat, source files, Git, patches,
  logs, delegated prompts, or unrelated tool calls. Do not store production,
  personal, or ambiguously classified credentials.
- If Project Memory is unavailable, say so rather than claiming it was
  searched or updated.
- Use `project_memory_stats` when the user asks for local usage or sanitized
  failure statistics. Treat reported server runs as approximate, not exact
  Codex session counts.
