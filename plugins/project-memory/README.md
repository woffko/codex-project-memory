# Project Memory plugin

This directory is the installable Codex plugin and Python MCP package. See the
[repository README](../../README.md) for installation, project enrollment,
hierarchical project-key routing, configuration, security boundaries, and
usage examples. Ordinary projects automatically become shared meta-projects
when enrolled child directories are added.

The stdio server can also be run directly:

```bash
python3 scripts/project_memory_mcp.py serve
```

Run its tests with:

```bash
python3 scripts/test_project_memory.py
```
