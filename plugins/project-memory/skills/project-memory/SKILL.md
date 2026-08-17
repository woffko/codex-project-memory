---
name: project-memory
description: Use local project-scoped MCP memory when troubleshooting, repeating operational actions, working with enrolled test equipment, locating project logs, or inspecting local usage and sanitized failures. Route meta-project work through explicitly mapped hierarchical project keys, search before repeating work, and save only a verified final solution.
---

# Project Memory workflow

Use the `project_memory` MCP tools only with a project key explicitly mapped by
the active workspace instructions. Keep a portable mapping in `AGENTS.md`, or
use a Git-excluded local `AGENTS.override.md` for machine-specific paths and
keys. A same-directory override replaces `AGENTS.md`, so it must repeat every
instruction from that directory that needs to remain active. Never create or
modify an instruction file merely because this skill was installed. Pass the
mapped key as `project` on every call. `project_root` remains available only
for legacy configurations and resolves only the root's default project. Never
use `project_root` to choose among multiple logical projects at the same path.

A project may have enrolled child projects. A child may use a nested directory
or the exact same canonical root as its parent. The parent holds shared memory;
a child holds implementation-specific memory. The parent role is derived from
its children, so an ordinary project becomes a meta-project automatically when
its first child is enrolled. Same-root projects still have separate databases
and usage statistics.

## Select the project

1. Determine the active project from the task and repository-path mapping in
   the active workspace instructions. When keys share one root, use the mapped
   component or task scope; the current path alone is insufficient.
2. Call `project_memory_status` for that exact project key.
3. If status names a `parent_project`, call status and search for that parent
   separately when the task may use shared procedures, test equipment, or
   infrastructure.
4. Do not use a sibling project's memory unless the task explicitly spans
   that sibling or the user asks for it.
5. If the task does not map unambiguously to a project key, ask before writing
   memory. Do not guess a key from a similar repository name.

## Before troubleshooting or repeating an operational action

1. Search the active project using the task, error signature, command, device
   name, or log purpose.
2. Search the declared parent project separately for relevant shared memory.
3. Reuse a stored solution only when its project, context, and verification
   still match.
4. After applying or evaluating a retrieved record, call
   `project_memory_mark_used` with `reused`, `helpful`, `not_applicable`, or
   `stale`. Retrieval alone does not prove that memory helped.

## Repetitions and final solutions

- Write component-specific candidates and solutions to the active child
  project. Write genuinely shared procedures to the parent project explicitly.
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
- Prefer the declared parent project for equipment shared by multiple child
  projects.
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
- Use `project_memory_stats` only when the user asks about local usage or
  failures. It reports aggregate project activity and sanitized error groups;
  MCP server runs are only an approximation of Codex sessions.
- When a memory tool returns an `error_id`, keep it available for local
  diagnosis, but do not invent or expose raw error details that were not
  returned.
- Deprecate stale records instead of deleting them. Record why the old solution
  is no longer valid.
- If the MCP server or mapped project is unavailable, say so; do not claim that
  memory was searched or updated.
