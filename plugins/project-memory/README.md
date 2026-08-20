# Project Memory plugin

This directory is the installable Codex plugin and Python MCP package. See the
[repository README](../../README.md) for installation, project enrollment,
hierarchical project-key routing, configuration, security boundaries, and
usage examples. The default hot path is adaptive `recall` followed by a
targeted `read` only when coverage, ambiguity, conflict, or evidence requires
it. Records retain lossless local history while exposing bounded `card`,
`action`, `evidence`, and `full` views. Ordinary projects automatically become shared meta-projects
when enrolled children are added. Children may use nested directories or the
same repository root with distinct project keys and databases. Local usage
aggregates and sanitized errors can be inspected without opening project
content databases:

Plugin installation does not create or modify `AGENTS.md` or
`AGENTS.override.md`. Keep portable routing in an existing `AGENTS.md`, or use
a Git-excluded local `AGENTS.override.md` for machine-specific routing. A
same-directory override replaces `AGENTS.md`; it does not extend it.

```bash
codex-project-memory-report summary --all --since 30d
codex-project-memory-report errors --all --last 20
```

The stdio server can also be run directly:

```bash
python3 scripts/project_memory_mcp.py serve
python3 scripts/project_memory_mcp.py serve --project Product/core --profile lean
```

Back up and migrate local content databases:

```bash
codex-project-memory migrate --all
```

Compare quality and cost locally:

```bash
codex-project-memory-bench \
  --queries testdata/recall_cases.json \
  --compare legacy,compact,balanced,auto,deep
```

Run its tests with:

```bash
python3 scripts/test_project_memory.py
python3 scripts/test_project_memory_metrics.py
python3 scripts/test_project_memory_adaptive.py
python3 scripts/test_project_memory_benchmark.py
```
