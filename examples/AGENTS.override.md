# Local Project Memory routing

This file contains machine-local routing guidance. Keep it untracked through
the target repository's `.git/info/exclude`; do not copy these mappings into a
portable `AGENTS.md`. Replace the neutral example paths and keys with the exact
locally enrolled values. Do not store credentials in this file.

**Warning:** in the same directory this file replaces `AGENTS.md`; Codex does
not merge the two. Copy all required same-directory instructions into this
override before using it. Installing Project Memory does not create or modify
this file automatically.

## Project routing

- Shared/meta-project memory: `ExampleSuite`.
- Files under `main/` use project `ExampleSuite/main`.
- Files under `c-port/` use project `ExampleSuite/c-port`.
- Files under `rust-port/` use project `ExampleSuite/rust-port`.
- Files and procedures shared by the whole workspace use project
  `ExampleSuite`; add an explicit child mapping for any other repository that
  needs separate memory.
- If several keys intentionally share the same repository root, route by task
  scope as well as path. For example, core-library work may use
  `ExampleSuite/core` while GUI work uses `ExampleSuite/gui`; do not use the
  root-only selector to distinguish them.
- At task start, determine the active project from the task and target paths.
- Write component-specific memory to the active child. Write genuinely shared
  memory explicitly to the parent.
- Do not search sibling projects unless the task spans them. If routing is
  ambiguous, ask before writing memory.

## Project Memory

Use Project Memory for recurring troubleshooting, verified procedures,
constraints, decisions, failure patterns, environment facts, and continuation
checkpoints. Call adaptive recall first; it searches the selected child and its
parent. Expand evidence only when recall reports ambiguity, conflict, omitted
evidence, or incomplete coverage. Remember only real occurrences and verified
outcomes. Keep raw logs and all non-test credentials out of ordinary memory.

Store credentials only for assets explicitly classified as test-only through
the approval-gated test-asset tools. Never reproduce a retrieved credential in
chat, source files, Git, patches, logs, metrics, checkpoints, provenance,
delegated prompts, or unrelated commands. If Project Memory is unavailable,
say so instead of claiming that it was searched or updated.
