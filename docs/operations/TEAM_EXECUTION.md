# Bounded team execution (M2)

**Experimental preview, `main` only, full storage only, off by default.** This
page is the operator reference for running allowlisted tools on behalf of a team
run. Design and acceptance matrix: [team foundations](../design/team-foundations.md#m2-bounded-execution-first-slice);
decision record: [ADR](../adr/team-collaboration.md).

Two executor backends share one interface, one authority model and one evidence
format (`BRAIN_EXECUTION_BACKEND`):

- **`subprocess`** (default) is **not a security boundary**. It gives every run its
  own clone and a stripped environment, but a tool still runs as the server user,
  can read what that user can read, and can open network connections. Enable it only
  for tools you would let any granted member run on the Brain host.
- **`container`** runs every step in a fresh, hardened Docker container from an
  operator-named execution environment: no network by default, read-only root, only
  the working copy mounted, secrets as read-only files, non-root, no capabilities,
  resource limits. Egress, when granted, goes only through an allowlisting proxy.
  See [Container backend](#container-backend) for what it does and does not contain.

## What a run can do

An execution is a fixed list of steps attached to an existing team run
(`POST /team/projects/{project}/tasks/{task}/runs`). Each step names a tool the
operator defined, optional arguments (only for tools that accept them) and optional
secret references. Brain:

1. clones the task's repository into `<BRAIN_EXECUTION_ROOT>/<run id>/repo`, checks
   out the task's base SHA on the run's own branch and removes the `origin` remote;
2. before every step, re-checks the lease, cancellation, the whole authority chain
   and the wall-clock budget, then runs the tool without a shell, with the working
   copy as `cwd`, a minimal environment, a timeout and an output cap (with the
   container backend: inside a container, with the working copy at `/workspace`);
3. records each step (argv, exit code, duration, output SHA-256, redacted excerpt)
   and the final base/head SHAs as `run.execution.*` events in the task ledger. The
   `run.execution.started` event also records the backend, the environment (name,
   image reference, pinned image ID, repository digests) and the egress profile
   (`none`, or `allowlist` with its hosts); each container step records its
   container name, network and the secret references mounted.

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
   `BRAIN_EXECUTION_EGRESS` is a comma-separated destination allowlist of `host` or
   `host:port` entries (default none); the subprocess backend refuses every egress
   request, the container backend enforces it through the
   [egress proxy](#egress-deny-by-default). The source repository must be under the
   allowed repository roots. For the container backend see also
   [its settings](#container-backend).

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
   Each effective grant and explicit revocation is recorded in the team activity
   feed as `execution.granted` / `execution.revoked` (administrators only).

## Requesting, inspecting and cancelling

```http
POST /team/projects/{project}/tasks/{task}/runs/{run}/execution
{"idempotency_key": "ci-lint-0001",
 "steps": [{"tool": "ruff", "args": ["brain"]}, {"tool": "tests", "secrets": ["pypi"]}],
 "tools": ["ruff", "tests"], "secret_refs": ["pypi"], "egress": [], "environment": "python",
 "budget": {"wall_seconds": 600, "steps": 2, "step_timeout_seconds": 300, "output_bytes": 65536}}
```

`tools`, `secret_refs` and `budget` default to what the steps need and the operator
ceilings. `environment` is required with the container backend (an unknown name, a
tool the environment does not allow, or an environment sent to the subprocess
backend is denied) and is part of the stored capabilities: a child run must use its
parent's environment. Only the principal that requested the run may request its execution. The
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
  references (never values), egress allowlist, environment, backend and budget. A
  child run can only narrow its parent's; a parent without an execution record grants
  nothing. If the operator switches the backend after a request, the run is revoked
  before its first step ("The executor backend changed since the request").
- **Secrets** are resolved from the server environment at step start and given to
  that step only: with the subprocess backend as `BRAIN_SECRET_<REF>`, with the
  container backend as read-only files `$BRAIN_SECRETS_DIR/<ref>` (never environment
  variables or argv), removed after the step. Values never enter the database, API
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
  terminates the process group (the container backend first kills the container). The terminal event says honestly what was in flight:
  `finished_unrecorded` (the step ran but could not be recorded), `terminated`, or
  `unknown` (the owner disappeared).
- **Lease and fencing.** One worker owns an execution through a lease with a
  fencing token. Every write is a compare-and-set on owner, token and expiry, so an
  owner whose lease expired cannot commit anything. On worker start (and before
  each execution) expired leases are fenced: the token is bumped, the run is released
  and marked `fenced`, and it is never resumed. `brain team execution recover` does
  the same by hand. With the container backend, recovery on worker start also removes
  every container and network labelled `brain.execution.run=<run id>`.
- **One status machine.** The only state is the run's `run.execution.*` events in
  `harness.agent_task_events`; the execution row holds request, capabilities and the
  lease, not a status column.

## Container backend

```bash
BRAIN_EXECUTION_BACKEND=container
BRAIN_EXECUTION_TOOLS='{"tests": {"argv": ["python", "-m", "pytest", "-q"]},
                        "ruff": {"argv": ["ruff", "check"], "allow_args": true}}'
BRAIN_EXECUTION_ENVIRONMENTS='{"python": {"image": "registry.example/brain-exec-python:3.12",
                                          "tools": ["tests", "ruff"],
                                          "cpus": "2", "memory": "2g", "pids": 512}}'
```

An **execution environment** names an image and the subset of
`BRAIN_EXECUTION_TOOLS` allowed in it (`cpus`, `memory` and `pids` optionally
override the defaults below). Tool `argv` is resolved inside the image. Every
execution must name one environment; an unknown environment is denied at request
time. At the start of a run the image is pinned by ID (`docker image inspect`); it is
never pulled (`--pull never`), so pre-pull it on the executor host. A missing image
fails the run in the `environment` phase before any step.

| Setting | Default | Meaning |
|---|---|---|
| `BRAIN_EXECUTION_BACKEND` | `subprocess` | `subprocess` or `container` |
| `BRAIN_EXECUTION_ENVIRONMENTS` | `{}` | JSON: name -> `{"image", "tools", "cpus"?, "memory"?, "pids"?}` |
| `BRAIN_EXECUTION_DOCKER` | `docker` | docker CLI executable |
| `BRAIN_EXECUTION_CONTAINER_USER` | worker `uid:gid` (else `65534:65534`) | numeric, non-root `uid[:gid]` |
| `BRAIN_EXECUTION_CONTAINER_CPUS` | `1` | `--cpus` |
| `BRAIN_EXECUTION_CONTAINER_MEMORY` | `1g` | `--memory` and `--memory-swap` (no swap) |
| `BRAIN_EXECUTION_CONTAINER_PIDS` | `256` | `--pids-limit` |
| `BRAIN_EXECUTION_CONTAINER_TMPFS` | `256m` | size of the `/tmp` tmpfs |
| `BRAIN_EXECUTION_EGRESS_PROXY_IMAGE` | empty | image for the egress proxy; empty = egress refused |
| `BRAIN_EXECUTION_EGRESS_UPSTREAM_NETWORK` | `bridge` | network the proxy uses to reach the internet |

Each step runs as:

```text
docker run --rm --init --name brain-exec-<run>-s<NNN> --network none
  --pull never --read-only --tmpfs /tmp:rw,nosuid,nodev,size=<tmpfs>
  --user <uid:gid> --cap-drop ALL --security-opt no-new-privileges
  --label brain.execution.run=<run> --env HOME=/tmp ...
  --mount type=bind,source=<root>/<run>/repo,target=/workspace --workdir /workspace
  --cpus <n> --memory <m> --memory-swap <m> --pids-limit <p> --log-driver none
  [--mount type=bind,source=<root>/<run>/secrets/step-<NNN>,target=/run/brain-secrets,readonly
   --env BRAIN_SECRETS_DIR=/run/brain-secrets]
  --entrypoint <argv[0]> <image id> <argv[1:]...>
```

The docker CLI inherits only the variables it needs to reach the daemon
(`DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`, TLS paths, `HOME`, `PATH`). The
container is killed and removed on timeout, on a failed revocation checkpoint and
after every step.

After a step has run, the working copy's `.git` is untrusted (hooks,
`core.fsmonitor` or filter drivers would execute on the host), so the final head is
read by `git status` **inside** a network-less, read-only container from the same
image. Environment images therefore need `git`.

### Egress: deny by default

Without egress the step has `--network none`. A run whose capabilities hold an
egress allowlist (requested `egress`, within `BRAIN_EXECUTION_EGRESS`) instead gets:

1. a per-run Docker network created with `--internal` (no route out), labelled with
   the run;
2. a sidecar proxy on that network with alias `brain-egress:3128`, started from
   `BRAIN_EXECUTION_EGRESS_PROXY_IMAGE` as
   `python -m brain.execution.egress --allow host:port ...` (hardened like a step),
   and additionally connected to `BRAIN_EXECUTION_EGRESS_UPSTREAM_NETWORK`;
3. `HTTP_PROXY` / `HTTPS_PROXY` (and lower-case forms) pointing at the proxy in every
   step container, with `NO_PROXY` empty.

The proxy (`brain/execution/egress.py`, standard library only) accepts
`CONNECT host:port` and absolute-form `http://` requests, nothing else. A destination
is allowed only when the host is listed exactly (no wildcards) with that port, or is
listed without a port and the request is for 80/443. It resolves names itself and
refuses a name that resolves to any non-global address (loopback, private,
link-local such as cloud metadata); IP literals listed explicitly are allowed. It
connects only to addresses it checked, and logs one JSON line per decision
(`docker logs brain-egress-<run>` while the run is alive). The step's own DNS does
not work on the internal network, so tools must use the proxy. The network and the
proxy are removed when the run ends.

`BRAIN_EXECUTION_EGRESS_PROXY_IMAGE` must contain the `brain` package (normally the
Brain image itself). Prefer a dedicated upstream network to one shared with Brain's
databases: the proxy refuses private destinations, but defence in depth is cheap.

### Deployment notes

- **Docker access is root-equivalent.** The worker needs the docker CLI and a daemon
  socket (or `DOCKER_HOST`). Anyone who controls the worker controls the host. Run the
  executor on a dedicated host or worker if you can.
- **Paths must match.** The daemon bind-mounts `<BRAIN_EXECUTION_ROOT>/<run>/repo` by
  path, so `BRAIN_EXECUTION_ROOT` must be the same path on the daemon host and in the
  worker (when the worker itself runs in a container, mount it at the identical
  path). A remote daemon that cannot see that path makes every step fail.
- **File ownership.** Secret files are written `0400` by the worker user. The
  container user must be able to read them and write the working copy: keep the
  default (the worker's own `uid:gid`) or set `BRAIN_EXECUTION_CONTAINER_USER` to a
  user that owns `BRAIN_EXECUTION_ROOT`.
- **Cleanup.** Every container and network carries `brain.execution.run=<run id>`;
  `docker ps --all --filter label=brain.execution.run` finds leftovers, and worker
  start-up recovery removes those of fenced runs.
- **Configuration health.** `brain.team.execution.execution_config_health()` returns
  a pure, docker-free description of the execution configuration (backend,
  environments, whether egress is enforced, missing secret variables). The team
  doctor's `execution_config` check is built on it and adds what only the worker
  process can see: the root exists, and the docker CLI (container backend) or the
  host tools (subprocess backend) resolve. The subprocess backend is reported as
  a warning because it has no filesystem or network boundary.
- **Changed files.** A completed execution records `changed_paths` (sorted,
  repository-relative, at most 5000) and `changed_paths_total`: what the run
  changed against the task base, tracked and untracked but not ignored. The
  container backend lists them inside a network-less container over a read-only
  mount. These paths feed M3 overlap detection (`overlaps` counts the warnings);
  if they cannot be read the execution still completes with `changed_paths: null`.
- **Dependencies.** M3 subtask dependencies are checked when an execution is
  requested and again when it starts; an unfinished dependency at start revokes
  it before a working copy is made.

## LLM providers: consent and budget

A step that may send repository content to an external LLM provider (an agent CLI,
a review tool calling a model API) has to say so. The execution request carries an
`llm` declaration next to `egress`:

```json
{"steps": [...], "egress": ["api.openai.com:443"],
 "llm": {"provider": "openai", "model": "gpt-5", "max_tokens": 200000, "max_cost_cents": 300}}
```

The operator names the providers this Brain may use, with their API hosts and,
optionally, the models it allows (no `models`: any model):

```bash
BRAIN_EXECUTION_LLM_PROVIDERS='{"openai": {"hosts": ["api.openai.com"], "models": ["gpt-5"]},
                                "anthropic": {"hosts": ["api.anthropic.com"]}}'
```

Egress to a host that belongs to an LLM provider (a configured provider's host, or a
well-known public LLM API host such as `api.openai.com` or `api.anthropic.com`) is
refused unless the execution declares that provider, so an egress allowlist cannot be
used to reach a model without the gates below. The subprocess backend refuses all
egress, so LLM use in practice needs the container backend with the egress proxy.

In **team state** a declared provider also needs both of these:

1. **Consent.** A team administrator records explicit consent for the project, provider
   and model (`*` covers every model the provider allows). The record keeps who
   consented and when, and is revocable. Revoking it (or removing the provider from
   settings) revokes running executions at their next step checkpoint.
2. **Budget.** The project needs an LLM budget: tokens and/or cost in cents, per UTC
   calendar `month` or `total`, with optional per-member limits (per initiator). Without
   a budget LLM use fails closed. Brain cannot meter tokens inside a step's TLS
   connection, so the declared `max_tokens` / `max_cost_cents` are **reserved in full**
   when the execution is requested. They are checked again at every checkpoint, so a
   lowered budget also revokes a running execution. An execution cancelled before it
   started reserves nothing. When a budget limits cost, the declaration must include
   `max_cost_cents`. A refusal names the scope (project or member), the amount already
   reserved, the limit and the declared amount.

In **solo state** (a team of one) the owner consents implicitly. No consent record is
needed, and a budget applies only if the owner set one. Adding the first member
switches to team state, and the consent and budget rules then apply to every new
request and every checkpoint.

A child run may declare LLM use only within its parent runs' declarations: the same
provider and model, and no higher `max_tokens` / `max_cost_cents`.

| | API (`/team/projects/{project}/execution/…`) | CLI (`brain team execution …`) |
|---|---|---|
| list consents | `GET llm-consents` (project read) | `consent list --project P` |
| consent | `PUT llm-consents` `{"provider", "model"}` (admin) | `consent grant --project P --provider X [--model M]` |
| revoke | `DELETE llm-consents/{provider}?model=M` (admin) | `consent revoke --project P --provider X [--model M]` |
| show budget and usage | `GET llm-budget` (project read) | `budget show --project P` |
| set budget | `PUT llm-budget` (admin) | `budget set --project P [--period month or total] [--max-tokens N] [--max-cost-cents N] [--per-member-max-tokens N] [--per-member-max-cost-cents N]` |
| clear budget | `DELETE llm-budget` (admin) | `budget clear --project P` |

Every change is an admin-audience activity event (`execution.llm_consented`,
`execution.llm_consent_revoked`, `execution.llm_budget_set`,
`execution.llm_budget_cleared`). The execution detail and `run.execution.started`
evidence record the declaration. The team doctor's `execution_config` check warns when
an egress host is an LLM API host that no configured provider owns, or when a provider
host is missing from `BRAIN_EXECUTION_EGRESS`. It fails when the providers setting is
invalid.

## Artifacts

Each execution keeps bounded outputs in `agent_run_execution_artifacts`, linked to the
run:

| Name | Kind | What |
|---|---|---|
| `step-NNN.log` | `log` | the tail of the step's output (already redacted by the backend) |
| `step-NNN-report.<ext>` | `test-report` | the file named by the tool's `report` key, if the step wrote it |
| `final.patch` | `patch` | the working copy against the task base: tracked changes (`git diff --full-index`, no external diff or text conversion) plus new untracked, non-ignored files; binary files appear as `Binary files … differ` stubs |

A tool declares its report file in `BRAIN_EXECUTION_TOOLS`, as a path relative to the
working copy:

```bash
BRAIN_EXECUTION_TOOLS='{"tests": {"argv": ["python", "-m", "pytest", "-q", "--junitxml=report.xml"],
                                  "report": "report.xml"}}'
```

The report is read without following symlinks and only inside the working copy. A step
that fails keeps its log. Before storage every artifact is redacted with the values of
the secrets the run used. It is then capped: `BRAIN_EXECUTION_ARTIFACT_MAX_BYTES`
(default 1 MiB) per artifact, keeping the tail for logs and the head otherwise, and
`BRAIN_EXECUTION_ARTIFACTS_MAX_BYTES` (default 8 MiB) per run. Artifacts beyond the
run cap are not stored. Each record keeps its size, original size, `truncated` flag
and SHA-256. The container backend builds the patch inside a network-less container
over a read-only mount. A patch that cannot be built is skipped and does not fail the
execution.

The execution detail (`GET …/runs/{run}/execution`) lists artifact metadata. For
project members with read access to the task:

- `GET …/runs/{run}/execution/artifacts` lists them.
- `GET …/runs/{run}/execution/artifacts/{id}` downloads one, always as an attachment,
  with `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`,
  `X-Artifact-SHA256` and `X-Artifact-Truncated`.

These are deliberately not `agent_task_artifacts`. That table belongs to a task, its
rows are reported by the client (path, kind, content hash), it stores no content and
it has no link to a run or an execution. Execution artifacts are produced by the
server from the run's own working copy, carry their bytes, and are fenced by the
execution lease like the rest of the run's evidence.

## Real-docker tests

The unit suite checks the exact docker argv against a fake CLI. The tests marked
`real_docker` in `tests/test_team_execution_container.py` drive a real daemon:

- step isolation: non-root user, read-only root, no network, secret files, timeout
  and container removal;
- egress denial: on the `--internal` network the proxy answers 403 to unlisted names,
  ports, metadata and private addresses, a direct connection fails, and the proxy logs
  each denial;
- head, changed paths and the final patch read inside the environment image.

No test contacts an outside host. The CI job `container-real-docker` (ubuntu-latest,
15-minute job timeout, 8-minute test step) builds both test images from
`tests/docker/execution.Dockerfile`. The `environment` target is `python:3.12-slim`
with git. The `egress-proxy` target holds only the `brain.execution` package. These
are test images, not production images. The job runs
`pytest tests/test_team_execution_container.py -m real_docker` with
`BRAIN_TEST_EXECUTION_IMAGE`, `BRAIN_TEST_EGRESS_PROXY_IMAGE` and
`BRAIN_TEST_REQUIRE_DOCKER=1`, which turns every skip into a failure.

Elsewhere these tests skip without a daemon, without the images or when run as root
(steps run as the caller's uid, as they do in production). To run them locally:

```bash
docker build -f tests/docker/execution.Dockerfile --target environment -t brain-exec-test-env .
docker build -f tests/docker/execution.Dockerfile --target egress-proxy -t brain-exec-test-proxy .
BRAIN_TEST_EXECUTION_IMAGE=brain-exec-test-env BRAIN_TEST_EGRESS_PROXY_IMAGE=brain-exec-test-proxy \
  python -m pytest tests/test_team_execution_container.py -m real_docker -v
```

## Why agent-chosen steps are deferred

Steps stay a fixed list chosen at request time. An agent that picks the next tool
call would need, inside the executor:

- an LLM loop that reads tool output, which is repository content and can carry prompt
  injection;
- a per-call policy decision;
- a review story for actions nobody approved one by one.

The safety properties above rely on the steps being known when the request is
authorized:

- the stored capability ceiling;
- the evidence in the task ledger;
- the budget reservation.

An agent can already run inside one step, as an allowlisted tool that declares its LLM
provider: its tool calls stay inside that step's container, egress profile and
budget. Planning across steps is a separate design. Dynamic steps would have to
re-authorize each proposed call against the stored capabilities, and record the
proposal and the decision as evidence.

## Known limits

- The subprocess backend is not containment: no filesystem, process or network
  isolation beyond a separate clone, `cwd` and environment. Proxy variables point at
  a dead address, which only stops tools that honour them. It refuses any egress.
- The container backend shares the host kernel: it is hardening, not a VM. A kernel
  or runtime escape defeats it; for hostile code use a dedicated executor host, or a
  sandboxed runtime (such as gVisor) as the daemon's default runtime.
- Egress control is by destination name and port at the proxy. It does not inspect
  TLS, so an allowed host can carry any traffic, and data can leave through any
  allowed destination.
- Images are pinned by ID per run, but environments name images by tag in settings;
  re-tagging an image changes what new runs get (the evidence records the ID).
- The real-docker CI job uses test images, not the production Brain image, and runs on
  GitHub's runner kernel and default runtime.
- LLM budgets are reservations of declared ceilings, not metered usage: Brain cannot see
  inside a step's TLS connection, so a tool that ignores its declared `max_tokens`
  can spend more at the provider. Use provider-side keys with their own spend limits.
- Steps are a fixed list chosen at request time; no agent decides the next step
  (see [Why agent-chosen steps are deferred](#why-agent-chosen-steps-are-deferred)).
- Artifacts live in the database with the run; there is no retention or pruning
  policy yet beyond the per-run cap.
- Effects outside the working copy (network calls, files the server user can write)
  cannot be recalled or made idempotent.
- The PostgreSQL race tests (several workers claiming one execution lease, two
  workers executing one run) run only where `TEST_TEAM_POSTGRES_URL` points at a
  disposable database; CI on SQLite skips them.

## What remains of M2

- Agent-driven dynamic steps (an agent proposing the next tool call within the
  stored capabilities), deferred; see [above](#why-agent-chosen-steps-are-deferred).
- Metered LLM usage (actual tokens and cost reported by the provider) instead of
  reserved ceilings.
- Artifact retention and cleanup policy for working copies and stored artifacts.
- External-effect idempotency (deduplicating side effects outside the clone).
- `brain doctor` reporting for orphaned runs (execution configuration is covered
  by the `execution_config` check).
- Running the PostgreSQL concurrency tests in CI.
- Dashboard UI and MCP tools for requesting and inspecting executions.
- Resuming a fenced run from its last recorded step (today: request a new run).
