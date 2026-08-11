# Contributing

1. Create a focused branch.
2. Keep runtime data, credentials, databases, and keys out of Git.
3. Run:

   ```bash
   python3 plugins/project-memory/scripts/test_project_memory.py
   python3 plugins/project-memory/scripts/test_project_memory_metrics.py
   python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/project-memory
   ```

4. Explain behavioral and storage-format changes in the pull request.

Keep the tracked plugin manifest on a canonical SemVer version. Do not commit
per-developer or timestamp cachebuster suffixes from feature branches. Local
development may add a cachebuster only to an untracked installation or staging
copy; bump the repository version once in an integration or release change.

Changes to encryption, project identity, enrollment, or secret-handling rules
must include regression tests and a migration note when applicable.
