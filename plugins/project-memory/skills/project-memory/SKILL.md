---
name: project-memory
description: Use local project-scoped MCP memory for recurring troubleshooting, verified procedures, constraints, decisions, failure patterns, environment facts, continuation checkpoints, test equipment, log locations, and private usage diagnostics.
---

# Project Memory workflow

Use Project Memory only with a project key explicitly mapped by active workspace
instructions or by the Project Memory routing note that the Claude Code
SessionStart hook derives from the local enrollment registry. Pass that key as
`project` on every call unless the MCP server is explicitly project-bound.
Legacy `project_root` selects only the default memory at that root and must not
be used to guess among same-root logical projects.

Codex: keep portable mappings in `AGENTS.md` and machine-specific paths and
keys in a Git-excluded `AGENTS.override.md`. A same-directory override replaces
rather than merges with `AGENTS.md`, so it must repeat every instruction from
that directory that must remain active.

Claude Code: the plugin's SessionStart hook supplies the registry mapping for
the session directory. Claude Code does not read `AGENTS.override.md`; put
task-scope routing that the registry cannot express in `CLAUDE.md` or a
Git-excluded `CLAUDE.local.md`. When work moves to another directory during a
session, run the read-only
`${XDG_DATA_HOME:-~/.local/share}/codex-project-memory/runtime/bin/codex-project-memory route --path DIR --format context`
to get its mapping; it opens no project database.

When the routing note reports a Git repository that is not enrolled, offer
enrollment once, and only when the task benefits from durable project
knowledge. Run the quoted `enroll` command only after the user agrees, and the
quoted `route --decline` command when the user refuses. Write no Project Memory
records for an unenrolled repository.

On resume or compaction the hook also lists the project keys and checkpoints
that this host session already used; continue with them unless the task moved.
Writes are attributed to the host session in the project audit:
`codex-project-memory audit --project KEY --current-session` lists what this
session wrote, for example to review, mark stale, or deprecate the records of
a session that went wrong.

Never create or modify an instruction file merely because this skill is
installed.

## Recall before repeating work

1. Call `project_memory_recall` with the mapped project, a focused query, and
   relevant `task_type` or `risk` hints. Parent search is included by default.
2. Apply a returned direct action only when its applicability, constraints,
   warnings, verification, version or device limits, and staleness state match.
3. Call `project_memory_read` only when recall reports ambiguity, conflict,
   omitted evidence, incomplete coverage, or a targeted expansion. Prefer
   `action` or `evidence`; use `full` only for maintenance or deep investigation.
4. Fold `reused`, `helpful`, `not_applicable`, or `stale` feedback into a later
   recall, or use the compatibility feedback tool.

Do not search siblings unless the task explicitly spans them. If no exact key
is mapped, ask before writing. If the server is unavailable, say so and do not
claim that memory was searched.

## Remember durable knowledge

Use `project_memory_remember` after real occurrences or verification:

- `occurrence` tracks repeated candidate work; a solution needs at least two
  qualifying occurrences and complete verification evidence;
- `upsert` stores structured constraints, decisions, failure patterns,
  environment facts, and eligible verified solutions;
- `failed_attempt` preserves a bounded important failure without placing full
  attempt history in the hot action payload;
- `checkpoint` stores short continuation state with a branch, commit, and TTL;
- `feedback` records whether recalled knowledge helped.

Store exact steps, applicability, constraints, warnings, verification, outcome,
versions or devices, and bounded provenance. Never invent missing rationale or
verification. Keep raw logs out of memory; store their stable location through
the compatibility log-location tool. Use `project_memory_report_stale` instead
of deleting knowledge that no longer applies.

Write implementation-specific records to the active child. Write genuinely
shared procedures and constraints to the declared parent.

## Test equipment

The user may authorize an enrolled project to retain credentials only for
equipment explicitly classified as test-only. Use the admin-profile
`project_memory_store_test_asset` tool with `test_only: true`, and place every
password, token, or key inside `secret_fields`. Retrieve it only when the active
task needs it through the approval-gated secret-read tool.

When a reviewed non-interactive Longrun command needs one stored test-asset
field on stdin, prefer `project_memory_stage_test_asset_for_longrun`. It
decrypts locally and returns only a short-lived one-time `stdin_secret_id`; the
secret value never enters the agent context. Stage a handle only when a Longrun
MCP server is connected to the current session to consume it. Pass that handle directly to
`longrun.start_job` or `longrun.run_and_wait`. Do not ask the user to run
`codex-longrun-secret` when a suitable encrypted test asset already exists;
that helper is only a fallback for a credential not yet enrolled in Project
Memory.

Use `project_memory_get_test_asset` only when the active operation genuinely
needs the plaintext value outside Longrun and the approved destination can
consume it without placing it in chat, argv, environment, source, or logs.

Never reproduce a retrieved secret in chat, ordinary memory, source files,
Git, patches, logs, metrics, checkpoints, provenance, delegated prompts, or
unrelated commands. Production, personal, and ambiguously classified secrets
remain prohibited.

## Relation to host memory

Built-in host memory, such as Claude Code auto-memory, stays the place for user
preferences, collaboration feedback, and cross-project notes. Store
project-scoped operational knowledge here, and do not duplicate one fact in
both stores.

## Maintenance

Use compatibility or admin tools only when the task requires legacy behavior,
statistics, backup, deprecation, or approved test-secret access. Usage metrics
are aggregate and private; they do not prove that a task was completed
correctly. Do not claim token or quality improvements without a labeled local
benchmark.
