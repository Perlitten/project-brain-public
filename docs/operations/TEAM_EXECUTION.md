# Bounded team execution (M2, first slice)

**Experimental preview, `main` only, full storage only, off by default.** This
page is the operator reference for running allowlisted tools on behalf of a team
run. Design and acceptance matrix: [team foundations](../design/team-foundations.md#m2-bounded-execution-first-slice);
decision record: [ADR](../adr/team-collaboration.md).

The subprocess backend shipped here is **not a security boundary**. It gives every
run its own clone and a stripped environment, but a tool still runs as the server
user, can read what that user can read, and can open network connections. Enable it
only for tools you would let any granted member run on the Brain host.

## What a run can do

An execution is a fixed list of steps attached to an existing team run
(`POST /team/projects/{project}/tasks/{task}/runs`). Each step names a tool the
operator defined, optional arguments (only for tools that accept them) and optional
secret references. Brain:

1. clones the task's repository into `<BRAIN_EXECUTION_ROOT>/<run id>/repo`, checks
   out the task's base SHA on the run's own branch and removes the `origin` remote;
2. before every step, re-checks the lease, cancellation, the whole authority chain
   and the wall-clock budget, then runs the tool without a shell, with the working
   copy as `cwd`, a minimal environment, a timeout and an output cap;
3. records each step (argv, exit code, duration, output SHA-256, redacted excerpt)
   and the final base/head SHAs as `run.execution.*` events in the task ledger.

Nothing is pushed, merged or published. The working copy stays on disk for review.

## Enabling it

Every layer below is required. Anything missing denies.

1. **Operator switch** (API and worker environment, then restart both):

   ```bash
   BRAIN_EXECUTION_ENABLED=true
   BRAIN_EXECUTION_ROOT=/srv/brain/executions          # dedicated directory, not inside a repository
   BRAIN_EXECUTION_TOOLS='{"tests": {"argv": ["python", "-m", "pytest", "-q"]},
                           "ruff": {"argv": ["ruff", "check"], "allow_args": true}}'
   BRAIN_EXECUTION_SECRETS='{"pypi": "PYPI_READ_TOKEN"}' # optional: reference -> server env var name
   ```

   Budget ceilings (defaults in brackets): `BRAIN_EXECUTION_MAX_WALL_SECONDS` [900],
   `BRAIN_EXECUTION_MAX_STEPS` [20], `BRAIN_EXECUTION_STEP_TIMEOUT_SECONDS` [300],
   `BRAIN_EXECUTION_MAX_OUTPUT_BYTES` [65536], `BRAIN_EXECUTION_LEASE_SECONDS` [60].
   `BRAIN_EXECUTION_EGRESS` is a comma-separated destination allowlist (default none);
   it has no effect with the subprocess backend, which refuses every egress request.
   The source repository must be under the allowed repository roots.

2. **Project policy** must include `execute`. Projects created without
   `--permission` allow every permission; a project created with an explicit list
   that omits `execute` can never execute.

3. **Role**: the run's human initiator must be a live, verified member with the
   Contributor or Reviewer role on the project. No role implies execution; a solo
   operator must first become a team member (`brain team init`, `brain team member
   add`) to grant themselves.

4. **Per-member grant** by a team administrator (or the host CLI):

   ```bash
   brain team execution grant  --project <id|name> --principal <id|name>
   brain team execution list   --project <id|name>
   brain team execution revoke --project <id|name> --principal <id|name>
   ```

   HTTP: `PUT` / `DELETE /team/projects/{project}/members/{principal}/execution`,
   `GET /team/projects/{project}/execution-grants`. A grant to a Reader is refused.
   A grant on one project does not carry over to another. Revoking the member, or
   removing them from the project, revokes the grant; re-adding the role does not
   revive it. If the role is later downgraded to Reader the grant stays on record
   but is ineffective until the role is restored.

## Requesting, inspecting and cancelling

```http
POST /team/projects/{project}/tasks/{task}/runs/{run}/execution
{"idempotency_key": "ci-lint-0001",
 "steps": [{"tool": "ruff", "args": ["brain"]}, {"tool": "tests", "secrets": ["pypi"]}],
 "tools": ["ruff", "tests"], "secret_refs": ["pypi"], "egress": [],
 "budget": {"wall_seconds": 600, "steps": 2, "step_timeout_seconds": 300, "output_bytes": 65536}}
```

`tools`, `secret_refs` and `budget` default to what the steps need and the operator
ceilings. Only the principal that requested the run may request its execution. The
request is committed, then queued as the `team_run_execution` job. Replaying the same
key and content returns the same record; different content under the same run
conflicts (request a new run instead).

- `GET .../execution` shows the derived state (`requested`, `running`, `completed`,
  `failed`, `revoked`, `cancelled`, `fenced`, `budget_exhausted`, `orphaned`) with
  the ledger events.
- `POST .../execution/cancel` is allowed to the requester, the run's initiator or a
  project administrator.

## Security model

**Effective authority is the intersection of every layer**, re-evaluated before each
tool operation and each ledger write:

operator switch ∩ project policy (`execute`) ∩ initiator role ∩ live per-member grant
∩ the base team authority (`authorize()`: live credentials, verified membership,
scopes, task binding, repository link, delegation chain) ∩ the run's stored
capabilities ∩ each parent run's stored capabilities ∩ the operator allowlists.

- **Capabilities are data.** Each execution stores its tool allowlist, secret
  references (never values), egress allowlist and budget. A child run can only
  narrow its parent's; a parent without an execution record grants nothing.
- **Secrets** are resolved from the server environment at step start and passed as
  `BRAIN_SECRET_<REF>` to that step only. Values never enter the database, API
  responses or step logs; output containing them is redacted. Tools inherit no other
  server variable except `PATH` (and Windows basics).
- **Arguments** are refused for tools without `allow_args`. Allowed arguments are
  checked lexically: no absolute or home paths, no drive letters, no `..`
  components (also after `=`), no control characters, at most 1024 characters.
  Flags are not filtered, so only set `allow_args` for tools whose every flag is
  acceptable.
- **Revocation checkpoints.** Revoking a grant, credential, membership or run, or
  removing `execute` from the policy, is seen at the next checkpoint. During a
  running step the probe re-checks every third of the lease interval (0.2 to 5 s) and
  terminates the process group. The terminal event says honestly what was in flight:
  `finished_unrecorded` (the step ran but could not be recorded), `terminated`, or
  `unknown` (the owner disappeared).
- **Lease and fencing.** One worker owns an execution through a lease with a
  fencing token. Every write is a compare-and-set on owner, token and expiry, so an
  owner whose lease expired cannot commit anything. On worker start (and before
  each execution) expired leases are fenced: the token is bumped, the run is released
  and marked `fenced`, and it is never resumed. `brain team execution recover` does
  the same by hand.
- **One status machine.** The only state is the run's `run.execution.*` events in
  `harness.agent_task_events`; the execution row holds request, capabilities and the
  lease, not a status column.

## Known limits

- The subprocess backend is not containment: no filesystem, process or network
  isolation beyond a separate clone, `cwd` and environment. Proxy variables point at
  a dead address, which only stops tools that honour them.
- Egress cannot be enforced, so any non-empty egress request is refused.
- Steps are a fixed list chosen at request time; no agent decides the next step.
- Effects outside the working copy (network calls, files the server user can write)
  cannot be recalled or made idempotent.
- The execution suite runs on SQLite and PostgreSQL, but sequentially: there is no
  test of two workers racing for one lease on PostgreSQL yet.

## What remains of M2

- A container backend (no network, read-only root, mounted secrets, resource
  limits) behind the same `ExecutorBackend` interface, and real egress enforcement.
- Named execution environments (images, toolchains) instead of host tools.
- Provider egress consent and an LLM token/cost budget for agent-driven runs.
- Agent-driven dynamic steps (an agent proposing the next tool call within the
  stored capabilities).
- Artifact upload/retention for working copies and logs, and cleanup policy.
- External-effect idempotency (deduplicating side effects outside the clone).
- `brain doctor` reporting for execution configuration and orphaned runs.
- PostgreSQL concurrency tests for leases and fencing.
- Dashboard UI and MCP tools for requesting and inspecting executions.
- Resuming a fenced run from its last recorded step (today: request a new run).
