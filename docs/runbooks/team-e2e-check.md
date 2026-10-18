# Team e2e check

`scripts/team_e2e_check.py` runs team mode end to end on disposable databases.
It starts from the schema and rows of the release production ran before team
mode (`c13d056`). It is opt-in: it is not part of `pytest` or normal CI.
Run it before you deploy team mode, and after a change to migrations, team
authorization, OAuth or restore.

## Run it

You need Docker with compose v2, and git with full history (for
`git archive c13d056`). You do not need Python, `psql` or `pg_dump` on the host.

```bash
scripts/team_e2e_docker.sh              # add --keep-image to reuse the runner image next time
```

The wrapper does the following:

1. It stages the working tree. Only tracked and unignored files are copied, so
   `.env` and other ignored secrets stay on the host.
2. It extracts `git archive c13d056` as the legacy tree.
3. It starts PostgreSQL 15 (pgvector) on `127.0.0.1:5448` and Redis on
   `127.0.0.1:6398` under compose project `brain-e2e`.
4. It builds the runner image, `brain-e2e-runner:local`. The image uses
   `postgresql-client-15`, the same major version as production.
5. It runs the check.
6. It copies the JSON report to `artifacts/team-e2e/latest/report.json`.
7. It removes the stack, its volumes and the image.

The exit status is the check's. A run takes a few minutes once the image is
built. All secrets are generated per run and redacted from the output.

The GitHub workflow **Team e2e (opt-in)** (`.github/workflows/team-e2e.yml`,
`workflow_dispatch` only) runs the same wrapper and uploads the report.

If the image build cannot reach the Debian or PGDG mirrors, use the host
network for the build. This happens on a Docker daemon inside WSL whose bridge
network has no outbound route:

```bash
TEAM_E2E_BUILD_NETWORK=host scripts/team_e2e_docker.sh
```

You can also run the script directly against your own disposable server. It
touches only databases named `brain_e2e_*` and refuses any other name:

```bash
git archive c13d056 | tar -x -C /tmp/legacy-c13d056
python scripts/team_e2e_check.py --database-url postgresql://USER:PASS@127.0.0.1:5448/postgres \
    --redis-url redis://127.0.0.1:6398/0 --legacy-tree /tmp/legacy-c13d056
```

The API runs on ports `18471` and `18472`; change them with `--api-port`.
`--keep-databases` leaves the databases in place for inspection.

## What it checks

Each check prints `PASS`, `FAIL`, `GAP` or `GAP-FIXED`.

| Scenario | Checks |
|---|---|
| 1 Legacy upgrade | `c13d056` creates its schema and representative rows: principals with named keys, repositories, decisions (including a NULL/global duplicate), rules, a learning, a queued job, an OAuth client with a refresh token, and the shared key. Then `init_db` from main runs. **Checks:** every legacy column is unchanged, apart from the documented supersede of the duplicate decision. New authorship columns are NULL and `version` is 1. `team_state()` is `solo` and the legacy job runs. The owner key, shared key and OAuth token still work. Editing a decision keeps its id, bumps `version`, writes one history row and records the editor in `updated_by`, with authorship still NULL. A stale `expected_version` gets 409. |
| 2 Idempotence | A second `init_db` changes no rows or columns. Repeating `brain team init`, `project add` and `repo attach` gives the same results. A repository cannot join a second project. A team with no members stays solo. |
| 3 Team path (HTTP) | **Admin invitation:** the CLI invitation turns the Brain into team state. The shared key and non-member keys are refused, and so is the legacy job. **API invitation:** it moves through invited → verified → active. The member signs in to the dashboard (`/api/web-session`). **Member access:** she reads her project and is denied on the other project and on admin routes. Her decision and rule become pending proposals (202, [governed memory](../MEMORY_REVIEW.md)); she cannot approve her own, and the admin's approval publishes the decision attributed to her. She records a learning. She is denied on the other project; a global decision is only a proposal. Her reviewed edit of a legacy decision uses CAS and leaves its authorship NULL. She creates a task and a run, and gets a run credential. **OAuth:** a member added by an operator and an invited member both complete team-scoped OAuth (PKCE), each bound to their own principal. **Revocation:** after the API revoke, her key, dashboard session, run credential and MCP are dead at once, and her pending proposals are withdrawn. The personal-scope OAuth client she approved is revoked with her (access 401, refresh refused). The same holds for CLI revoke, including the OAuth access token and refresh token. After the last member is revoked the Brain stays in team state. `brain team dissolve` refuses without `--yes`, and `--yes` returns the Brain to solo while revoked keys and her personal OAuth token stay dead. |
| 4 Backup and restore | `backup_runner.py` produces a dump with no plaintext keys. `restore_database.py` restores it fenced: every credential and OAuth refresh is refused. `brain team reconcile` prints a digest, and a wrong digest leaves the fence in place. `--attest --keep-credentials` brings the old credentials back. The default attest revokes them all, and a re-minted admin key with the same issuer and subject works. A plain `psql` restore is **not** fenced and honours restored keys (documented). |
| 5 Freshness | `/search`, `/context` and the MCP `search_code` reply (member key, Streamable HTTP) carry a `freshness` envelope. |

The check does not cover:

- the Next.js web proxy;
- real indexing, the worker or Neo4j (the backup runs with `--allow-missing neo4j`);
- browser OAuth consent (the consent form is posted directly).

## Known gaps

A `GAP` check means a gap has been reported but not fixed. It does not fail
the run. `GAP-FIXED` means the gap no longer reproduces: turn that check into
a plain `check` and remove the entry here.

None at the moment: every check is a plain `check`.
