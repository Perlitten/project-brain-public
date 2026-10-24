"""Core runtime fields: environment, paths, API keys, web/public URLs."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import field_validator


class SettingsCore:
    # General Settings
    ENVIRONMENT: str = "local"

    BRAIN_STORAGE_MODE: Literal["full", "lite"] = "full"

    BRAIN_LITE_DIR: str = ".brain"

    BRAIN_LITE_MAX_CHUNKS: int = 50_000

    # Unset: documentation is available locally and disabled in production.
    API_DOCS_ENABLED: Optional[bool] = None

    BRAIN_BUILD_SHA: str = "unknown"

    BRAIN_SOURCE_DIGEST: str = "unknown"

    # Off by default - DEBUG drives SQLAlchemy echo, which logs every statement
    # (and params) to stdout. Opt in locally with DEBUG=true when needed.
    DEBUG: bool = False

    REPO_ROOT: str = "."

    # Context packs are generated artifacts, not source files. Keep their
    # writable storage separate from TARGET_REPO_PATH, which is mounted
    # read-only in production.
    CONTEXT_PACK_OUTPUT_DIR: Optional[str] = None

    # One rollback switch for all agent-facing v2 contracts. ``shadow`` computes
    # bounded v2 metrics but keeps the byte-compatible v1 response client-side.
    BRAIN_AGENT_CONTEXT_V2_MODE: str = "on"

    BRAIN_WORKER_POOLS_V2_ENABLED: bool = False

    REPORT_OUTPUT_DIR: Optional[str] = None

    # Orchestration API (scheduler, GitHub Actions, external triggers)
    PROJECT_BRAIN_API_KEY: Optional[str] = None

    # Local surfaces (stdio MCP server, `brain` CLI) act as the operator by default: direct,
    # unattributed memory writes. A named pbk_ credential here makes them act as that principal
    # instead, under the same team-state governance as the HTTP API (docs/MEMORY_REVIEW.md).
    BRAIN_LOCAL_PRINCIPAL_KEY: Optional[str] = None

    # Comma-separated addresses or CIDRs of proxies (the web dashboard and any
    # reverse proxy in front of the API) whose X-Forwarded-For the invitation
    # and single sign-on throttles believe. Empty: the socket peer is the client.
    BRAIN_TRUSTED_PROXY_IPS: str = ""

    # Mirrors nginx `client_max_body_size 25m` for deployments that expose
    # uvicorn directly (docker compose publishes 8000 without the proxy).
    # 0 disables the application-level cap.
    # Off by default: query text can contain source or secrets. Metadata-only
    # history remains available without this opt-in; old queries cannot be recovered.
    # Seeds the persisted installation-wide capture preference only once.
    REQUEST_TELEMETRY_CAPTURE_QUERY: bool = False

    API_MAX_REQUEST_BODY_BYTES: int = 25 * 1024 * 1024

    BRAIN_STALE_CONTEXT_POLICY: Literal["warn_and_serve", "strict_block"] = "warn_and_serve"

    BRAIN_STALE_DIFF_MAX_FILES: int = 64

    BRAIN_STALE_DIFF_TIMEOUT_S: float = 1.0

    BRAIN_STALE_SOURCE_MAX_BYTES: int = 256_000

    # Index-age SLO reported by /health (per repository); older indexes mark the
    # service `degraded`. 0 disables the check.
    BRAIN_FRESHNESS_MAX_AGE_S: int = 604_800

    # Public URL of the web UI (apps/web, deployed separately). GET / reports it
    # and the retired /dashboard paths redirect to it; unset means no UI link.
    BRAIN_WEB_URL: Optional[str] = None

    # Native MCP over Streamable HTTP at /mcp (apps/api/mcp_http.py).
    BRAIN_MCP_HTTP_ENABLED: bool = True

    # Public base URL of this API. Turns on the MCP OAuth authorization server
    # (it is the token issuer, so it must be what clients reach); when unset it
    # falls back to https://BRAIN_PUBLIC_HOST, and with neither OAuth stays off.
    BRAIN_PUBLIC_URL: Optional[str] = None

    BRAIN_PUBLIC_HOST: Optional[str] = None

    # mock | anthropic | google | any OpenAI-compatible preset: openai, nvidia,
    # openrouter, groq, together, deepseek, mistral, ollama, lmstudio,
    # openai_compatible (aliases: custom, openai-compatible).
    DEFAULT_LLM_PROVIDER: str = "mock"

    DEFAULT_EMBEDDING_PROVIDER: str = "mock"

    # Target product repo for embeddings / re-index (override via env)
    TARGET_REPO_PATH: str = "."

    # Extra repository roots accepted by repo_path validation (comma-separated).
    ALLOWED_REPO_ROOTS: Optional[str] = None

    # Also accept the process cwd and the system tempdir as repo roots (eval
    # scratch repos, local dev). Unset = on everywhere except production.
    ALLOW_SCRATCH_REPO_ROOTS: Optional[bool] = None

    # Allow O(n) JSON cosine fallback only in explicit dev mode
    ALLOW_EMBEDDING_JSON_FALLBACK: bool = False

    # Team M2 bounded execution (docs/operations/TEAM_EXECUTION.md). Off by default; enabling it
    # also needs a per-member execution grant on the project. Each run gets its own clone under
    # BRAIN_EXECUTION_ROOT. Tools: JSON object name -> {"argv": [...], "allow_args": bool}.
    # Secrets: JSON object reference -> name of a server environment variable (the value never
    # leaves the server). Egress: comma-separated host[:port] entries; the subprocess backend
    # enforces no network boundary, so any requested egress is refused there.
    BRAIN_EXECUTION_ENABLED: bool = False

    BRAIN_EXECUTION_ROOT: Optional[str] = None

    BRAIN_EXECUTION_TOOLS: str = "{}"

    BRAIN_EXECUTION_SECRETS: str = "{}"

    BRAIN_EXECUTION_EGRESS: str = ""

    BRAIN_EXECUTION_MAX_WALL_SECONDS: int = 900

    BRAIN_EXECUTION_MAX_STEPS: int = 20

    BRAIN_EXECUTION_STEP_TIMEOUT_SECONDS: int = 300

    BRAIN_EXECUTION_MAX_OUTPUT_BYTES: int = 65536

    BRAIN_EXECUTION_LEASE_SECONDS: int = 60

    # Executor backend: "subprocess" (default) or "container" (each step in a hardened container
    # via the docker CLI). Environments: JSON object name -> {"image": ..., "tools": [...],
    # optional "cpus", "memory", "pids"}; the container backend requires every execution to name
    # one. Container user: numeric non-root uid[:gid] (default: the server's own uid:gid).
    # Egress proxy image: an image with Project Brain installed (normally the Brain image itself);
    # empty means the container backend refuses egress too.
    BRAIN_EXECUTION_BACKEND: str = "subprocess"

    BRAIN_EXECUTION_ENVIRONMENTS: str = "{}"

    BRAIN_EXECUTION_DOCKER: str = "docker"

    BRAIN_EXECUTION_CONTAINER_USER: str = ""

    BRAIN_EXECUTION_CONTAINER_CPUS: str = "1"

    BRAIN_EXECUTION_CONTAINER_MEMORY: str = "1g"

    BRAIN_EXECUTION_CONTAINER_PIDS: int = 256

    BRAIN_EXECUTION_CONTAINER_TMPFS: str = "256m"

    BRAIN_EXECUTION_EGRESS_PROXY_IMAGE: str = ""

    BRAIN_EXECUTION_EGRESS_UPSTREAM_NETWORK: str = "bridge"

    # LLM providers executions may declare (docs/operations/TEAM_EXECUTION.md#llm-providers-consent-and-budget):
    # JSON name -> {"hosts": ["api.example.com", ...], "models": [...] (optional; empty = any)}. In team state a
    # run that declares one also needs a team administrator's consent and a project LLM budget.
    BRAIN_EXECUTION_LLM_PROVIDERS: str = "{}"

    # Execution artifacts (step log tails, test reports, the final patch) stored in the database: the cap for
    # one artifact and for all artifacts of one run.
    BRAIN_EXECUTION_ARTIFACT_MAX_BYTES: int = 1048576

    BRAIN_EXECUTION_ARTIFACTS_MAX_BYTES: int = 8388608

    # Skip LLM plan/checklist synthesis (faster context packs; retrieval-only mode)
    CONTEXT_PACK_SKIP_PLAN_LLM: bool = False

    # Owner alert channel. Keep credentials in the production .env only.
    TELEGRAM_ALERTS_ENABLED: bool = False

    TELEGRAM_ALERT_BOT_TOKEN: Optional[str] = None

    TELEGRAM_ALERT_CHAT_ID: Optional[str] = None

    TELEGRAM_ALERT_COOLDOWN_SECONDS: int = 24 * 60 * 60

    TELEGRAM_ALERT_TIMEOUT_S: float = 20.0

    BRAIN_TELEGRAM_DEFAULT_REPO: Optional[str] = None

    # Web dashboard sign-in, managed from its Settings screen (apps/api/routers/web_login.py).
    # Empty user = open sign-in. The hash is scrypt; the version rotates on every
    # change so the dashboard's session cookies stop matching.
    WEB_LOGIN_USER: Optional[str] = None

    WEB_LOGIN_PASSWORD_HASH: Optional[str] = None

    WEB_LOGIN_VERSION: Optional[str] = None

    # Post-merge reindex webhook (POST /webhooks/git-merge, called by GitHub Actions).
    PROJECT_BRAIN_WEBHOOK_TOKEN: Optional[str] = None

    # Only merges onto this ref enqueue a reindex. Default matches the branch
    # the reindex-on-push example triggers on; it posts $GITHUB_REF, so
    # changing the trigger branch alone is enough unless the workflow is
    # pinned to a different ref.
    PROJECT_BRAIN_WEBHOOK_REF: str = "refs/heads/main"

    # Blank = the indexed repository (TARGET_REPO_PATH); see _default_repo_paths.
    PROJECT_BRAIN_GIT_REPO_PATH: str = ""

    # A reindex job that names a source revision (the git-merge webhook always
    # does) fetches origin and detaches the checkout at that revision before
    # indexing. Off: the job fails when the checkout HEAD is not that revision,
    # as it did before. A read-only or dirty checkout fails with a clear error
    # either way; it is never modified.
    BRAIN_WEBHOOK_FETCH_SOURCE: bool = True

    BRAIN_WEBHOOK_FETCH_TIMEOUT_S: int = 120

    # Origin poll (brain/workers/origin_poll.py): every interval the scheduler
    # runs `git ls-remote` against each attached repository's origin, records
    # the remote head and enqueues the same reindex the webhook would when it
    # moved. 0 disables the poll. Intervals under an hour should divide 60.
    BRAIN_ORIGIN_POLL_INTERVAL_S: int = 300

    BRAIN_ORIGIN_POLL_TIMEOUT_S: int = 30

    # Brain-owned writable directory for bare mirrors of attached repositories
    # (brain/indexers/source_mirror.py). A checkout that is read-only, has no
    # origin or is not git at all (a tar-deployed /app) is fetched into
    # <dir>/<name>-<hash>.git and indexed from the mirror commit. Blank
    # disables mirrors: such a checkout then fails as before.
    BRAIN_SOURCE_MIRROR_DIR: str = ""

    # Brain-owned writable directory for repositories connected from the web UI
    # by URL (brain/indexers/git_connect.py). Each one is cloned into
    # <dir>/<name>-<hash> and then registered, indexed and polled like any
    # other attached repository. The directory is implicitly an allowed
    # repository root. Blank disables connecting by URL.
    BRAIN_MANAGED_REPOS_DIR: str = ""

    # JSON map of repository path -> origin, for sources with no usable
    # `origin` remote: {"/app": "https://github.com/org/repo.git"} or
    # {"/app": {"url": "...", "branch": "main"}}. Credentials embedded in a URL
    # stay server-side: they are passed on the git command line only and are
    # redacted from every stored or reported value.
    BRAIN_ORIGIN_REMOTES: str = ""

    # Where database backups land, as this process sees it (``brain team doctor``
    # reads the newest backup_manifest.json from scripts/backup_runner.py or
    # brain_db.sql.gz from deploy/server_up.sh; nothing is written). Blank:
    # backup status is reported as unknown.
    BRAIN_BACKUP_DIR: str = ""

    BRAIN_BACKUP_MAX_AGE_HOURS: int = 48

    @field_validator("BRAIN_AGENT_CONTEXT_V2_MODE")
    @classmethod
    def validate_agent_context_v2_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"off", "shadow", "on"}:
            raise ValueError("BRAIN_AGENT_CONTEXT_V2_MODE must be off, shadow, or on")
        return normalized
