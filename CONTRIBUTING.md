# Contributing

1. Create a focused branch.
2. Keep runtime data, credentials, databases, and keys out of Git.
3. Run:

   ```bash
   python3 plugins/project-memory/scripts/test_project_memory.py
   python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/project-memory
   ```

4. Explain behavioral and storage-format changes in the pull request.

Changes to encryption, project identity, enrollment, or secret-handling rules
must include regression tests and a migration note when applicable.
