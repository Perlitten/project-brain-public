# Good first issues — prepared texts

Owner: copy these into GitHub issues when ready. Each is scoped to be done in
one PR and verified locally.

1. **`brain doctor` should check Neo4j auth, not just connectivity.**
   Today a wrong password surfaces as an index-time failure. Repro: set a bad
   `NEO4J_PASSWORD`, run `brain doctor` → it passes. Expected: doctor fails
   with a pointer to `.env`. Files: `apps/cli/commands/doctor.py` (or wherever
   the neo4j probe lives), `tests/test_doctor*.py`.

2. **Web UI: empty-state CTA on `/quality`.**
   Other empty pages link to `/setup`; `/quality` doesn't. Copy the pattern
   from `apps/web/app/runs/page.tsx` (`EmptyState` + CTA). Verify: demo mode
   off → page shows CTA; screenshot in PR.

3. **`brain health` output should be valid JSON with `--json`.**
   Add a `--json` flag emitting `{service: {ok, detail}}` for scripting.
   Files: `apps/cli/commands/` health command + a test.

4. **README quickstart: Windows path note for `TARGET_REPO_PATH`.**
   Windows users hit `\` escaping issues in `.env`. Add a 3-line note +
   quote the path. Docs-only change.

5. **Migrate `apps/api/helpers.py` imports to the new locations.**
   The shim re-exports `brain/dashboard/helpers.py` and
   `apps/api/mcp_status.py`; switch direct importers and delete the shim.
   Mechanical — grep `from apps.api.helpers` / `apps.api.helpers` and repoint.

6. **Coverage report: fail CI below a floor.**
   CI uploads `coverage.xml` but doesn't gate. Add `--cov-fail-under=70` to
   the pytest step and pin the floor in `pyproject.toml`. May need a few
   cheap tests or a lower floor first — measure, then set.

7. **`docs/EVAL.md` is dense — add a 5-line "run it in 3 commands" header.**
   New readers shouldn't have to parse the whole page to run one golden task.

8. **MCP tool annotations audit.**
   `apps/mcp_server/protocol.py` annotates destructive/idempotent flags;
   check all 13 tools in `server.py` carry correct annotations and write a
   test asserting the set stays complete.
