# Project Memory plugin

This directory is the installable Codex plugin and Python MCP package. See the
[repository README](../../README.md) for installation, project enrollment,
hierarchical project-key routing, configuration, security boundaries, and
usage examples. Ordinary projects automatically become shared meta-projects
when enrolled child directories are added. Local usage aggregates and
sanitized errors can be inspected without opening project content databases:

Keep exact path-to-key routing in a Git-excluded local `AGENTS.override.md`,
not in the repository's portable `AGENTS.md`.

```bash
codex-project-memory-report summary --all --since 30d
codex-project-memory-report errors --all --last 20
```

The stdio server can also be run directly:

```bash
python3 scripts/project_memory_mcp.py serve
```

Run its tests with:

```bash
python3 scripts/test_project_memory.py
python3 scripts/test_project_memory_metrics.py
```
