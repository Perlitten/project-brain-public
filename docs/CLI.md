# CLI Usage

Once the package is installed in editable mode, the `brain` command-line executable is registered and available.

Version check without CLI dependencies:

```bash
python -m brain.version
```

### Health Checks
To verify connection health across all local or remote database dependencies (PostgreSQL, Redis, Neo4j):
```bash
brain health
```

### Auditing a Repository
To scan a target repository structure and compile the initial read-only codebase audit:
```bash
brain audit --repo <path_to_repository>
```
* **Default behavior**: If `--repo` is omitted, it uses `REPO_ROOT` from `.env` (defaults to the current project directory).
* **Output**: The scan generates a structured report at `reports/initial-audit.md` relative to the Project Brain root.

---


