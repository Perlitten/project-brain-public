import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from brain.release_gate.approval import (
    MAX_EVIDENCE_BYTES,
    _read_bounded_json,
    validate_release_approval_payload,
)
from brain.release_identity import read_baked_source_manifest


def _env_files() -> tuple[str, ...]:
    """dotenv files the settings load, lowest to highest precedence.

    The Brain install's own ``.env`` (next to pyproject.toml in a checkout) is
    the base layer so MCP clients launching the server from an unrelated
    working directory still find the install's configuration. The process
    CWD's ``.env`` then overrides it (backwards compatible), and an explicit
    ``BRAIN_ENV_FILE`` path wins over everything — the supported way to point
    an installed wheel at its configuration."""
    files: list[str] = []
    package_env = Path(__file__).resolve().parents[2] / ".env"
    if package_env.is_file():
        files.append(str(package_env))
    files.append(".env")
    explicit = os.getenv("BRAIN_ENV_FILE", "").strip()
    if explicit:
        files.append(explicit)
    return tuple(files)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_files(), env_file_encoding="utf-8", extra="ignore")

    # General Settings
    ENVIRONMENT: str = "local"
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
    BRAIN_AGENT_CONTEXT_V2_MODE: str = "off"
    BRAIN_WORKER_POOLS_V2_ENABLED: bool = False
    AGENT_LOCATOR_MAX_BYTES: int = 2_400
    AGENT_RUNTIME_CONTEXT_MAX_BYTES: int = 26_000
    AGENT_ASK_INPUT_MAX_BYTES: int = 18_000
    AGENT_ASK_OUTPUT_MAX_BYTES: int = 2_000
    AGENT_IMPACT_MAX_BYTES: int = 8_000
    AGENT_RETRIEVAL_DEADLINE_S: float = 15.0
    AGENT_CONTEXT_DEADLINE_S: float = 30.0
    AGENT_ASK_DEADLINE_S: float = 25.0
    AGENT_IMPACT_DEADLINE_S: float = 10.0
    REPORT_OUTPUT_DIR: Optional[str] = None

    # Orchestration API (scheduler, GitHub Actions, external triggers)
    PROJECT_BRAIN_API_KEY: Optional[str] = None
    # Mirrors nginx `client_max_body_size 25m` for deployments that expose
    # uvicorn directly (docker compose publishes 8000 without the proxy).
    # 0 disables the application-level cap.
    API_MAX_REQUEST_BODY_BYTES: int = 25 * 1024 * 1024
    WORKER_REDIS_PREFIX: str = "brain:worker"
    WORKER_POOL_NAME: str = "maintenance"
    WORKER_JOB_LEASE_SECONDS: int = 60
    WORKER_JOB_TIMEOUT_SECONDS: int = 60 * 60
    # Individual quality-first background jobs may opt into a longer timeout,
    # but the worker still enforces this hard ceiling.
    WORKER_MAX_JOB_TIMEOUT_SECONDS: int = 6 * 60 * 60
    WORKER_HEARTBEAT_TTL_SECONDS: int = 45
    # Total depth budget across queued + processing + retrying per queue prefix.
    # Enqueue is rejected with QueueDepthExceeded once reached; 0 disables.
    WORKER_QUEUE_MAX_DEPTH: int = 5000
    INDEX_FILE_CONCURRENCY: int = 4
    INDEX_PROVIDER_CONCURRENCY: int = 2
    INDEX_EMBEDDING_BATCH_SIZE: int = 8
    INDEX_GRAPH_BATCH_SIZE: int = 256
    INDEX_PROGRESS_INTERVAL_SECONDS: float = 10.0
    INDEX_REPAIR_CHUNK_LIMIT: int = 500
    HARNESS_STALE_REAPER_ENABLED: bool = True
    HARNESS_STALE_REAPER_BATCH_SIZE: int = 500

    # Change-lab sandbox for validation commands: "auto" (docker if the daemon
    # + image are present, else unshare namespaces, else off), "docker",
    # "unshare", or "off". "off" keeps the previous unverified-isolation mode.
    LAB_SANDBOX_MODE: str = "auto"
    LAB_SANDBOX_IMAGE: str = "debian:bookworm-slim"
    LAB_SANDBOX_PIDS_LIMIT: int = 256
    LAB_SANDBOX_MEMORY_MB: int = 2048
    LAB_SANDBOX_CPUS: float = 2.0
    # Extra host paths (JSON list or CSV of absolute dirs) mounted read-only /
    # re-exposed inside the sandbox — deployment-specific toolchains.
    LAB_SANDBOX_EXTRA_MOUNTS: list[str] = []

    # Public URL of the web UI (apps/web, deployed separately). GET / reports it
    # and the retired /dashboard paths redirect to it; unset means no UI link.
    BRAIN_WEB_URL: Optional[str] = None

    # PostgreSQL Configuration
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5433
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres_password"
    POSTGRES_DB: str = "brain_db"
    POSTGRES_POOL_SIZE: int = 10
    POSTGRES_MAX_OVERFLOW: int = 10
    POSTGRES_POOL_TIMEOUT_SECONDS: float = 15.0

    # Database connection URL will be assembled if not provided in environment
    DATABASE_URL: Optional[str] = None

    # Redis Configuration
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    REDIS_URL: Optional[str] = None

    # Neo4j Configuration
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j_password"

    # LLM / AI Configuration
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    GOOGLE_API_KEY: Optional[str] = None
    NVIDIA_API_KEY: Optional[str] = None
    NVIDIA_LLM_MODEL: str = "meta/llama-3.1-70b-instruct"
    NVIDIA_EMBEDDING_MODEL: str = "nvidia/nv-embedcode-7b-v1"
    NVIDIA_SUMMARIZER_MODEL: str = "meta/llama-3.1-8b-instruct"
    # Keys for the other OpenAI-compatible presets (brain/llm/presets.py).
    OPENROUTER_API_KEY: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None
    TOGETHER_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None
    MISTRAL_API_KEY: Optional[str] = None
    # Per-task model overrides. Unset = derived from the active provider: the
    # small/fast model (SUMMARIZER_MODEL or preset default) for classification,
    # summarization and insight, the main model (LLM_MODEL or preset default)
    # for synthesis. For nvidia that is exactly the historical 8b/70b split;
    # keep self-diagnosis (insight) on the low-latency route — the operational
    # snapshot is large enough that slower reasoning models can exceed the SLA.
    LLM_TASK_CLASSIFICATION_MODEL: Optional[str] = None
    LLM_TASK_SUMMARIZATION_MODEL: Optional[str] = None
    LLM_TASK_SYNTHESIS_MODEL: Optional[str] = None
    LLM_TASK_INSIGHT_MODEL: Optional[str] = None
    # mock | anthropic | google | any OpenAI-compatible preset: openai, nvidia,
    # openrouter, groq, together, deepseek, mistral, ollama, lmstudio,
    # openai_compatible (aliases: custom, openai-compatible).
    DEFAULT_LLM_PROVIDER: str = "mock"
    DEFAULT_EMBEDDING_PROVIDER: str = "mock"
    # Universal OpenAI-compatible endpoint settings. They apply to the provider
    # selected by DEFAULT_LLM_PROVIDER and override its preset defaults.
    # LLM_BASE_URL must include the version path (https://host/v1).
    LLM_BASE_URL: Optional[str] = None
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: Optional[str] = None
    SUMMARIZER_MODEL: Optional[str] = None
    # Same for DEFAULT_EMBEDDING_PROVIDER. EMBEDDING_BASE_URL / EMBEDDING_API_KEY
    # fall back to LLM_BASE_URL / LLM_API_KEY when both slots use the same provider.
    EMBEDDING_BASE_URL: Optional[str] = None
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_MODEL: Optional[str] = None
    # Single configured embedding dimension (0 = auto from provider/preset).
    # Required for an embedding model the preset does not know. Changing it
    # rebuilds the pgvector column — re-index everything afterwards.
    EMBEDDING_DIMENSION: int = 0
    # Client-side per-input char cap and per-request batch size (0 = preset/default).
    EMBEDDING_MAX_INPUT_CHARS: int = 0
    # Retry budget for LLM/embedding API calls: total attempts per request and
    # base delay (seconds) for exponential backoff. Covers transient 429/5xx
    # and network blips from providers like NVIDIA.
    LLM_MAX_RETRIES: int = 5
    LLM_RETRY_BASE_DELAY_S: float = 2.0
    EMBEDDING_BATCH_SIZE: int = 0
    # Send the asymmetric-retrieval ``input_type`` field (NVIDIA-style).
    # Unset = preset behaviour (nvidia: yes, others: no).
    EMBEDDING_SEND_INPUT_TYPE: Optional[bool] = None
    # Target product repo for embeddings / re-index (override via env)
    TARGET_REPO_PATH: str = "."
    # Grafify graph JSON export (override via env GRAFIFY_OUTPUT_PATH)
    GRAFIFY_OUTPUT_PATH: Optional[str] = None
    # Obsidian vault directory for rules/decisions import
    OBSIDIAN_VAULT_PATH: Optional[str] = None
    # Allow O(n) JSON cosine fallback only in explicit dev mode
    ALLOW_EMBEDDING_JSON_FALLBACK: bool = False
    # Deterministic retrieval: skip LLM for task classification/keyword extraction
    RETRIEVAL_USE_LLM_CLASSIFICATION: bool = False
    # Optional LLM rerank for top-30 -> top-10 (deterministic fallback always available)
    RETRIEVAL_USE_LLM_RERANK: bool = False
    # v6 P2 two-stage semantic rerank (top-30 -> top-10 rescore). Gated within v6 so the
    # P1-conservative vs P2-semantic selection can be A/B'd; no effect when v6 is off.
    RETRIEVAL_V6_SEMANTIC_RERANK: bool = True
    # v6 P3 file cards: deterministic per-file "card" + card_vector retrieval channel.
    # Off by default; requires RETRIEVAL_V6_ENABLED. v6-off stays frozen v5.
    RETRIEVAL_V6_FILE_CARDS_ENABLED: bool = False
    # Ablation switches (only meaningful when file cards enabled).
    RETRIEVAL_V6_CARD_RECALL: bool = True
    RETRIEVAL_V6_CARD_RERANK: bool = True
    # Default card text format: "code_shaped" | "prose". Chosen by the P3 micro-test -
    # prose surfaced required files better on nv-embedcode (mean card rank 3.5 vs 5.05).
    RETRIEVAL_CARD_TEXT_FORMAT: str = "prose"
    # Moderate card_vector channel weight in fusion (never card-first).
    RETRIEVAL_CARD_VECTOR_WEIGHT: float = 0.9
    # Card embedding provider (pluggable; "" = inherit DEFAULT_EMBEDDING_PROVIDER).
    # Architecture supports a second provider for P3.5 A/B without code changes.
    RETRIEVAL_CARD_EMBEDDING_PROVIDER: Optional[str] = None
    # v6 P2 two-stage rerank: intermediate prune depth (top-100 -> top-30 -> top-10)
    RETRIEVAL_RERANK_PRUNE_LIMIT: int = 30
    # Hard timeout (s) for the optional LLM rerank stage before deterministic fallback
    RETRIEVAL_RERANK_TIMEOUT_S: float = 20.0
    # Retrieval v6 (P0): surface-aware candidate generation + top-100 internal pool.
    # When False the pipeline behaves byte-identically to the frozen Ranking v5 baseline.
    RETRIEVAL_V6_ENABLED: bool = False
    # Internal recall-stage pool depth fed into deterministic rerank (v6 widen).
    RETRIEVAL_POOL_LIMIT: int = 100
    # Precision pack depth - pack output stays at 10 regardless of pool depth.
    RETRIEVAL_PACK_LIMIT: int = 10
    # Minimum candidates each routed surface keeps in the recall pool before global fill.
    RETRIEVAL_SURFACE_MIN_QUOTA: int = 3
    # Skip LLM plan/checklist synthesis (faster context packs; retrieval-only mode)
    CONTEXT_PACK_SKIP_PLAN_LLM: bool = False
    # Per-dependency timeouts (seconds) for retrieval pipeline
    RETRIEVAL_TIMEOUT_VECTOR_S: float = 10.0
    RETRIEVAL_TIMEOUT_NEO4J_S: float = 8.0
    RETRIEVAL_TIMEOUT_LEXICAL_S: float = 5.0
    # Optional ColBERT late-interaction channel.  Every gate is false/zero by
    # default: the NVIDIA dense path remains authoritative until a measured
    # canary satisfies the cutover gates in docs/runbooks/lfm-colbert-cutover.md.
    LATE_INTERACTION_ENABLED: bool = False
    LATE_INTERACTION_DUAL_WRITE_ENABLED: bool = False
    LATE_INTERACTION_SHADOW_ENABLED: bool = False
    # Offline paired evaluation disables persistence so curated fixtures never
    # masquerade as production shadow traffic. Real shadow requests keep the
    # durable recorder enabled.
    LATE_INTERACTION_SHADOW_PERSIST_ENABLED: bool = True
    LATE_INTERACTION_RERANK_ENABLED: bool = False
    LATE_INTERACTION_CANARY_PERCENT: float = 0.0
    # Release-scoped safety authorization. Experiment authorization permits
    # measured dual-write/shadow only; production approval additionally permits
    # user-visible rerank/canary. The shared bindings prevent a stale `.env`
    # acknowledgement from silently carrying across a build/model/reindex.
    LATE_INTERACTION_EXPERIMENT_AUTHORIZED: bool = False
    LATE_INTERACTION_PRODUCTION_GATES_PASSED: bool = False
    LATE_INTERACTION_APPROVED_REPOSITORY_ID: Optional[int] = None
    LATE_INTERACTION_APPROVED_BUILD_SHA: Optional[str] = None
    LATE_INTERACTION_APPROVED_SOURCE_DIGEST: Optional[str] = None
    LATE_INTERACTION_APPROVED_MODEL_REVISION: Optional[str] = None
    LATE_INTERACTION_APPROVED_INDEX_REVISION: Optional[str] = None
    LATE_INTERACTION_APPROVED_LINEAGE_ID: Optional[str] = None
    LATE_INTERACTION_APPROVED_IDENTITY_DIGEST: Optional[str] = None
    LATE_INTERACTION_APPROVED_DOCUMENT_COUNT: Optional[int] = None
    LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH: Optional[str] = None
    LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256: Optional[str] = None
    LATE_INTERACTION_PROVIDER_URL: str = "http://127.0.0.1:8089"
    LATE_INTERACTION_ALLOW_REMOTE_PROVIDER: bool = False
    LATE_INTERACTION_MODEL: str = "LiquidAI/LFM2.5-ColBERT-350M-GGUF"
    LATE_INTERACTION_MODEL_REVISION: str = "bc240003aba07253e261a8aaf0d2c9683318a967"
    LATE_INTERACTION_DIMENSION: int = 128
    LATE_INTERACTION_QUERY_MAX_TOKENS: int = 32
    LATE_INTERACTION_DOCUMENT_MAX_TOKENS: int = 512
    LATE_INTERACTION_TIMEOUT_S: float = 20.0
    LATE_INTERACTION_RERANK_TIMEOUT_S: float = 2.0
    LATE_INTERACTION_DUAL_WRITE_TIMEOUT_S: float = 5.0
    LATE_INTERACTION_DUAL_WRITE_MAX_CONSECUTIVE_FAILURES: int = 3
    LATE_INTERACTION_RERANK_LIMIT: int = 50
    LATE_INTERACTION_MAX_RERANK_CHUNKS: int = 500
    LATE_INTERACTION_MIN_CANDIDATE_COVERAGE: float = 1.0
    LATE_INTERACTION_SCORE_WEIGHT: float = 0.25
    LATE_INTERACTION_STORAGE_DTYPE: str = "float16"
    # Optional co-located matrix/rerank service. GPU/PyLate and CPU/llama.cpp
    # backends share this contract and remain inert by default.
    LATE_INTERACTION_REMOTE_ENABLED: bool = False
    LATE_INTERACTION_REMOTE_URL: str = "http://127.0.0.1:8090"
    LATE_INTERACTION_REMOTE_TOKEN: Optional[str] = None
    LATE_INTERACTION_REMOTE_MODEL: str = "LiquidAI/LFM2.5-ColBERT-350M"
    LATE_INTERACTION_REMOTE_MODEL_REVISION: str = "59633c2e31717b3502343ff566bee9fda3261943"
    LATE_INTERACTION_REMOTE_TIMEOUT_S: float = 2.0
    LATE_INTERACTION_REMOTE_MUTATION_TIMEOUT_S: float = 120.0
    LATE_INTERACTION_REMOTE_MAX_CANDIDATES: int = 500
    LATE_INTERACTION_REMOTE_MAX_DOCUMENTS: int = 64
    LATE_INTERACTION_REMOTE_SYNC_BATCH_SIZE: int = 64
    LATE_INTERACTION_REMOTE_SOURCE_PAGE_SIZE: int = 256
    LATE_INTERACTION_REMOTE_MAX_PAYLOAD_BYTES: int = 2_000_000
    LATE_INTERACTION_SEARCH_CANDIDATE_LIMIT: int = 50
    LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION: Optional[str] = None
    # Explicit asynchronous slow lane. This is intentionally separate from the
    # online/shadow/canary gates above: only authenticated worker jobs can use
    # it, and each run reconciles the exact corpus before applying LFM scores.
    LATE_INTERACTION_DEEP_ENABLED: bool = False
    LATE_INTERACTION_DEEP_TIMEOUT_S: float = 120.0
    LATE_INTERACTION_DEEP_VECTOR_LIMIT: int = 200
    LATE_INTERACTION_DEEP_NOTIFY: bool = True
    NIGHTLY_DEEP_MAINTENANCE_ENABLED: bool = False
    NIGHTLY_DEEP_MAINTENANCE_REPO_PATH: str = "/app"
    NIGHTLY_DEEP_MAINTENANCE_NOTIFY: bool = True
    NIGHTLY_DEEP_MAINTENANCE_QUALITY_QUERIES: int = 3
    # Test/migration escape hatch only. Production requests must bind every
    # document/candidate to a SHA-256 content hash.
    LATE_INTERACTION_REMOTE_ALLOW_EMPTY_CONTENT_HASH: bool = False
    # Proactive insight loop. Manual job/API stays available; this gate is for
    # scheduled automation and tells operators whether the loop should run itself.
    PROACTIVE_INSIGHTS_ENABLED: bool = False
    PROACTIVE_INSIGHTS_LLM_ENABLED: bool = False
    PROACTIVE_INSIGHTS_MAX_SNAPSHOT_CHARS: int = 16000
    PROACTIVE_INSIGHTS_TIMEOUT_S: float = 75.0
    # Memory consolidation loop (L1 episodic → L2 pipeline → L3 learnings).
    # Manual run via worker job "memory_consolidation" stays available; this
    # gate is for scheduled automation.
    MEMORY_CONSOLIDATION_ENABLED: bool = False
    MEMORY_CONSOLIDATION_REQUIRE_APPROVAL: bool = True
    PROACTIVE_INSIGHTS_LLM_CACHE_TTL_SECONDS: int = 0

    # Closed-loop self diagnosis. Deterministic probes always run first; the LLM
    # may only synthesize evidence already present in that snapshot.
    SELF_DIAGNOSIS_ENABLED: bool = False
    SELF_DIAGNOSIS_USE_LLM: bool = True
    SELF_DIAGNOSIS_JOB_MAX_ATTEMPTS: int = 3
    SELF_DIAGNOSIS_RESULT_TTL_SECONDS: int = 7 * 24 * 60 * 60
    SELF_DIAGNOSIS_MIN_AVG_RECALL: float = 0.70

    # Owner alert channel. Keep credentials in the production .env only.
    TELEGRAM_ALERTS_ENABLED: bool = False
    TELEGRAM_ALERT_BOT_TOKEN: Optional[str] = None
    TELEGRAM_ALERT_CHAT_ID: Optional[str] = None
    TELEGRAM_ALERT_COOLDOWN_SECONDS: int = 6 * 60 * 60
    TELEGRAM_ALERT_TIMEOUT_S: float = 20.0
    BRAIN_TELEGRAM_DEFAULT_REPO: Optional[str] = None

    # In-process scheduler (brain/workers/scheduler.py), runs inside every worker.
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_TICK_SECONDS: int = 30
    # A slot missed while no worker ran still fires once if it is at most this old.
    SCHEDULER_CATCHUP_WINDOW_S: int = 6 * 60 * 60
    # /health turns degraded when a job's last success is older than its interval + this.
    SCHEDULER_STALE_GRACE_S: int = 2 * 60 * 60
    SCHEDULER_JOB_MAX_ATTEMPTS: int = 3
    WORKER_RETRY_BASE_DELAY_S: int = 5
    WORKER_RETRY_MAX_DELAY_S: int = 300
    # Optional dead-man switch per scheduled job (e.g. healthchecks.io), hit on success.
    DEADMAN_URL_NIGHTLY_MAINTENANCE: Optional[str] = None
    DEADMAN_URL_HEALTH_CHECK: Optional[str] = None
    DEADMAN_URL_SELF_DIAGNOSIS: Optional[str] = None
    DEADMAN_URL_BENCHMARK: Optional[str] = None
    # Post-merge reindex webhook (POST /webhooks/git-merge, called by GitHub Actions).
    PROJECT_BRAIN_WEBHOOK_TOKEN: Optional[str] = None
    PROJECT_BRAIN_GIT_REPO_PATH: str = "/app"

    @field_validator("POSTGRES_POOL_SIZE")
    @classmethod
    def validate_postgres_pool_size(cls, value: int) -> int:
        if not 1 <= value <= 50:
            raise ValueError("POSTGRES_POOL_SIZE must be between 1 and 50")
        return value

    @field_validator("POSTGRES_MAX_OVERFLOW")
    @classmethod
    def validate_postgres_max_overflow(cls, value: int) -> int:
        if not 0 <= value <= 50:
            raise ValueError("POSTGRES_MAX_OVERFLOW must be between 0 and 50")
        return value

    @field_validator("POSTGRES_POOL_TIMEOUT_SECONDS")
    @classmethod
    def validate_postgres_pool_timeout(cls, value: float) -> float:
        if not 0 < value <= 300:
            raise ValueError("POSTGRES_POOL_TIMEOUT_SECONDS must be greater than 0 and at most 300")
        return value

    @model_validator(mode="after")
    def validate_indexing_pool_capacity(self):
        if self.POSTGRES_POOL_SIZE + self.POSTGRES_MAX_OVERFLOW < 2:
            raise ValueError("Repository indexing locks require at least two PostgreSQL connections")
        return self

    @field_validator("INDEX_FILE_CONCURRENCY", "INDEX_PROVIDER_CONCURRENCY", "INDEX_EMBEDDING_BATCH_SIZE",
                     "INDEX_GRAPH_BATCH_SIZE", "INDEX_REPAIR_CHUNK_LIMIT")
    @classmethod
    def validate_index_bounds(cls, value: int) -> int:
        if not 1 <= value <= 1024:
            raise ValueError("Indexing bounds must be between 1 and 1024")
        return value

    @field_validator("INDEX_PROGRESS_INTERVAL_SECONDS")
    @classmethod
    def validate_index_interval(cls, value: float) -> float:
        if not 0.1 <= value <= 300:
            raise ValueError("Indexing progress interval must be between 0.1 and 300 seconds")
        return value

    @field_validator("WORKER_JOB_TIMEOUT_SECONDS", "WORKER_MAX_JOB_TIMEOUT_SECONDS")
    @classmethod
    def validate_worker_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("worker timeouts must be positive")
        return value

    @field_validator("BRAIN_AGENT_CONTEXT_V2_MODE")
    @classmethod
    def validate_agent_context_v2_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"off", "shadow", "on"}:
            raise ValueError("BRAIN_AGENT_CONTEXT_V2_MODE must be off, shadow, or on")
        return normalized

    @field_validator("WORKER_POOL_NAME")
    @classmethod
    def validate_worker_pool_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"fast", "maintenance", "deep"}:
            raise ValueError("WORKER_POOL_NAME must be fast, maintenance, or deep")
        return normalized

    @field_validator(
        "AGENT_LOCATOR_MAX_BYTES",
        "AGENT_RUNTIME_CONTEXT_MAX_BYTES",
        "AGENT_ASK_INPUT_MAX_BYTES",
        "AGENT_ASK_OUTPUT_MAX_BYTES",
        "AGENT_IMPACT_MAX_BYTES",
    )
    @classmethod
    def validate_agent_budget(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("agent byte budgets must be positive")
        return value

    @field_validator("LATE_INTERACTION_CANARY_PERCENT")
    @classmethod
    def validate_late_interaction_canary_percent(cls, value: float) -> float:
        if not 0.0 <= value <= 100.0:
            raise ValueError("LATE_INTERACTION_CANARY_PERCENT must be between 0 and 100")
        return value

    @field_validator(
        "EMBEDDING_DIMENSION",
        "EMBEDDING_MAX_INPUT_CHARS",
        "EMBEDDING_BATCH_SIZE",
        mode="before",
    )
    @classmethod
    def normalize_blank_embedding_integer(cls, value):
        # ``EMBEDDING_DIMENSION=`` in an env file means "auto", not a parse error.
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0
        return value

    @field_validator("EMBEDDING_DIMENSION", "EMBEDDING_MAX_INPUT_CHARS", "EMBEDDING_BATCH_SIZE")
    @classmethod
    def validate_embedding_integer(cls, value: int) -> int:
        if value < 0:
            raise ValueError("embedding size settings must be >= 0 (0 = auto)")
        return value

    @field_validator("EMBEDDING_SEND_INPUT_TYPE", mode="before")
    @classmethod
    def normalize_blank_optional_bool(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator(
        "LATE_INTERACTION_APPROVED_REPOSITORY_ID",
        "LATE_INTERACTION_APPROVED_DOCUMENT_COUNT",
        mode="before",
    )
    @classmethod
    def normalize_blank_late_interaction_optional_integer(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("LATE_INTERACTION_MIN_CANDIDATE_COVERAGE", "LATE_INTERACTION_SCORE_WEIGHT")
    @classmethod
    def validate_late_interaction_ratio(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("late-interaction ratios must be between 0 and 1")
        return value

    @field_validator(
        "LATE_INTERACTION_DIMENSION",
        "LATE_INTERACTION_QUERY_MAX_TOKENS",
        "LATE_INTERACTION_DOCUMENT_MAX_TOKENS",
        "LATE_INTERACTION_RERANK_LIMIT",
        "LATE_INTERACTION_MAX_RERANK_CHUNKS",
        "LATE_INTERACTION_DUAL_WRITE_MAX_CONSECUTIVE_FAILURES",
        "LATE_INTERACTION_REMOTE_MAX_CANDIDATES",
        "LATE_INTERACTION_REMOTE_MAX_DOCUMENTS",
        "LATE_INTERACTION_REMOTE_SYNC_BATCH_SIZE",
        "LATE_INTERACTION_REMOTE_SOURCE_PAGE_SIZE",
        "LATE_INTERACTION_REMOTE_MAX_PAYLOAD_BYTES",
        "LATE_INTERACTION_SEARCH_CANDIDATE_LIMIT",
        "LATE_INTERACTION_DEEP_VECTOR_LIMIT",
        "NIGHTLY_DEEP_MAINTENANCE_QUALITY_QUERIES",
    )
    @classmethod
    def validate_late_interaction_positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("late-interaction dimensions, token limits and rerank limit must be positive")
        return value

    @field_validator(
        "LATE_INTERACTION_TIMEOUT_S",
        "LATE_INTERACTION_RERANK_TIMEOUT_S",
        "LATE_INTERACTION_DUAL_WRITE_TIMEOUT_S",
        "LATE_INTERACTION_REMOTE_TIMEOUT_S",
        "LATE_INTERACTION_REMOTE_MUTATION_TIMEOUT_S",
        "LATE_INTERACTION_DEEP_TIMEOUT_S",
    )
    @classmethod
    def validate_late_interaction_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("late-interaction timeouts must be positive")
        return value

    @field_validator("LATE_INTERACTION_STORAGE_DTYPE")
    @classmethod
    def validate_late_interaction_dtype(cls, value: str) -> str:
        if value != "float16":
            raise ValueError("LATE_INTERACTION_STORAGE_DTYPE currently supports only float16")
        return value

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: Optional[str], info) -> str:
        if isinstance(v, str) and v:
            return v
        data = info.data
        user = data.get("POSTGRES_USER", "postgres")
        password = data.get("POSTGRES_PASSWORD", "postgres_password")
        host = data.get("POSTGRES_HOST", "localhost")
        port = data.get("POSTGRES_PORT", 5433)
        db = data.get("POSTGRES_DB", "brain_db")
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def assemble_redis_connection(cls, v: Optional[str], info) -> str:
        if isinstance(v, str) and v:
            return v
        data = info.data
        host = data.get("REDIS_HOST", "localhost")
        port = data.get("REDIS_PORT", 6379)
        password = data.get("REDIS_PASSWORD")
        if password:
            return f"redis://:{password}@{host}:{port}/0"
        return f"redis://{host}:{port}/0"

    @model_validator(mode="after")
    def _reject_default_secrets_in_production(self):
        """The dev defaults below are fine locally, but must never reach a
        production deploy. Fail fast at startup if they slip through."""
        if self.ENVIRONMENT.lower() == "production":
            insecure = {
                "POSTGRES_PASSWORD": "postgres_password",
                "NEO4J_PASSWORD": "neo4j_password",
            }
            offenders = [name for name, default in insecure.items() if getattr(self, name) == default]
            if offenders:
                raise ValueError("Refusing to start in production with default credentials: " + ", ".join(offenders))
        return self

    @model_validator(mode="after")
    def _validate_late_interaction_provider_url(self):
        parsed = urlparse(self.LATE_INTERACTION_PROVIDER_URL)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LATE_INTERACTION_PROVIDER_URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("LATE_INTERACTION_PROVIDER_URL must not contain credentials")
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.hostname.lower() not in loopback_hosts and not self.LATE_INTERACTION_ALLOW_REMOTE_PROVIDER:
            raise ValueError(
                "Non-loopback LATE_INTERACTION_PROVIDER_URL requires "
                "LATE_INTERACTION_ALLOW_REMOTE_PROVIDER=true"
            )
        self.LATE_INTERACTION_PROVIDER_URL = self.LATE_INTERACTION_PROVIDER_URL.rstrip("/")
        return self

    @model_validator(mode="after")
    def _validate_late_interaction_remote_url(self):
        parsed = urlparse(self.LATE_INTERACTION_REMOTE_URL)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LATE_INTERACTION_REMOTE_URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("LATE_INTERACTION_REMOTE_URL must not contain credentials")
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.hostname.lower() not in loopback_hosts and not self.LATE_INTERACTION_ALLOW_REMOTE_PROVIDER:
            raise ValueError(
                "Non-loopback LATE_INTERACTION_REMOTE_URL requires "
                "LATE_INTERACTION_ALLOW_REMOTE_PROVIDER=true"
            )
        if self.LATE_INTERACTION_REMOTE_ENABLED and not self.LATE_INTERACTION_REMOTE_TOKEN:
            raise ValueError("LATE_INTERACTION_REMOTE_ENABLED requires LATE_INTERACTION_REMOTE_TOKEN")
        self.LATE_INTERACTION_REMOTE_URL = self.LATE_INTERACTION_REMOTE_URL.rstrip("/")
        return self

    @model_validator(mode="after")
    def _validate_deep_lane(self):
        if self.WORKER_MAX_JOB_TIMEOUT_SECONDS < self.WORKER_JOB_TIMEOUT_SECONDS:
            raise ValueError(
                "WORKER_MAX_JOB_TIMEOUT_SECONDS cannot be below "
                "WORKER_JOB_TIMEOUT_SECONDS"
            )
        deep_active = (
            self.LATE_INTERACTION_DEEP_ENABLED
            or self.NIGHTLY_DEEP_MAINTENANCE_ENABLED
        )
        if not deep_active:
            return self
        if not self.LATE_INTERACTION_REMOTE_ENABLED:
            raise ValueError(
                "the LFM deep lane requires LATE_INTERACTION_REMOTE_ENABLED"
            )
        if self.LATE_INTERACTION_APPROVED_REPOSITORY_ID is None:
            raise ValueError(
                "the LFM deep lane requires an approved repository id"
            )
        if not (self.LATE_INTERACTION_APPROVED_LINEAGE_ID or "").strip():
            raise ValueError(
                "the LFM deep lane requires an approved corpus lineage id"
            )
        if not self.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH.strip():
            raise ValueError(
                "NIGHTLY_DEEP_MAINTENANCE_REPO_PATH cannot be blank"
            )
        return self

    @model_validator(mode="after")
    def _validate_late_interaction_release_gates(self):
        experiment_active = any(
            (
                self.LATE_INTERACTION_ENABLED,
                self.LATE_INTERACTION_DUAL_WRITE_ENABLED,
                self.LATE_INTERACTION_SHADOW_ENABLED,
            )
        )
        traffic_active = (
            self.LATE_INTERACTION_RERANK_ENABLED
            or self.LATE_INTERACTION_CANARY_PERCENT > 0
        )
        if not experiment_active and not traffic_active:
            return self
        if experiment_active and not self.LATE_INTERACTION_EXPERIMENT_AUTHORIZED:
            raise ValueError(
                "active LFM experiment flags require "
                "LATE_INTERACTION_EXPERIMENT_AUTHORIZED=true"
            )
        if traffic_active and not self.LATE_INTERACTION_PRODUCTION_GATES_PASSED:
            raise ValueError(
                "LFM rerank/canary requires "
                "LATE_INTERACTION_PRODUCTION_GATES_PASSED=true"
            )
        if self.ENVIRONMENT.lower() == "production" and not self.LATE_INTERACTION_REMOTE_ENABLED:
            raise ValueError("active production LFM requires the authenticated remote service")

        build_sha = self.BRAIN_BUILD_SHA.strip()
        source_digest = self.BRAIN_SOURCE_DIGEST.strip()
        if self.ENVIRONMENT.lower() == "production":
            manifest = read_baked_source_manifest()
            if manifest is None:
                raise ValueError(
                    "active production LFM requires the baked source manifest"
                )
            build_sha = str(manifest["source_revision"]).strip()
            source_digest = str(manifest["content_digest"]).strip()
        if (
            build_sha == "unknown"
            or self.LATE_INTERACTION_APPROVED_BUILD_SHA != build_sha
        ):
            raise ValueError("LFM authorization is not bound to the immutable build")
        if (
            re.fullmatch(r"[0-9a-fA-F]{64}", source_digest) is None
            or self.LATE_INTERACTION_APPROVED_SOURCE_DIGEST != source_digest
        ):
            raise ValueError(
                "LFM authorization is not bound to the immutable source digest"
            )
        if (
            self.LATE_INTERACTION_APPROVED_REPOSITORY_ID is None
            or self.LATE_INTERACTION_APPROVED_REPOSITORY_ID < 1
        ):
            raise ValueError(
                "active LFM requires LATE_INTERACTION_APPROVED_REPOSITORY_ID"
            )
        model_revision = (
            self.LATE_INTERACTION_REMOTE_MODEL_REVISION
            if self.LATE_INTERACTION_REMOTE_ENABLED
            else self.LATE_INTERACTION_MODEL_REVISION
        )
        if self.LATE_INTERACTION_APPROVED_MODEL_REVISION != model_revision:
            raise ValueError("LFM authorization is not bound to the configured model revision")

        expected_index = self.LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION
        if not expected_index:
            raise ValueError(
                "active LFM requires LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION"
            )
        if re.fullmatch(r"r[0-9]+", expected_index) is None:
            raise ValueError("active LFM index revision must use the rN format")
        if self.LATE_INTERACTION_APPROVED_INDEX_REVISION != expected_index:
            raise ValueError("LFM authorization is not bound to the expected index revision")
        lineage_id = (self.LATE_INTERACTION_APPROVED_LINEAGE_ID or "").strip()
        if not lineage_id or len(lineage_id) > 128:
            raise ValueError("active LFM requires an approved corpus lineage id")
        identity_digest = self.LATE_INTERACTION_APPROVED_IDENTITY_DIGEST or ""
        if re.fullmatch(r"[0-9a-fA-F]{64}", identity_digest) is None:
            raise ValueError("active LFM requires an approved corpus identity digest")
        if (
            self.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT is None
            or self.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT < 1
        ):
            raise ValueError("active LFM requires a positive approved document count")

        if traffic_active:
            evidence_hash = self.LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256 or ""
            if re.fullmatch(r"[0-9a-fA-F]{64}", evidence_hash) is None:
                raise ValueError(
                    "LFM production traffic requires a SHA-256 evidence bundle digest"
                )
            evidence_value = self.LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH or ""
            if not evidence_value.strip():
                raise ValueError(
                    "LFM production traffic requires an evidence bundle path"
                )
            report_root = Path(
                self.REPORT_OUTPUT_DIR
                or Path(__file__).resolve().parents[2] / "reports"
            ).resolve()
            evidence_path = Path(evidence_value)
            if not evidence_path.is_absolute():
                evidence_path = report_root / evidence_path
            evidence_path = evidence_path.absolute()
            try:
                relative_parts = evidence_path.relative_to(report_root).parts
            except ValueError as exc:
                raise ValueError(
                    "LFM evidence bundle must be inside REPORT_OUTPUT_DIR"
                ) from exc
            current_path = report_root
            for part in relative_parts:
                current_path = current_path / part
                if current_path.is_symlink():
                    raise ValueError("LFM evidence bundle must not use symlinks")
            evidence_path = evidence_path.resolve()
            try:
                evidence_path.relative_to(report_root)
            except ValueError as exc:
                raise ValueError(
                    "LFM evidence bundle must be inside REPORT_OUTPUT_DIR"
                ) from exc
            if not evidence_path.is_file() or evidence_path.is_symlink():
                raise ValueError(
                    "LFM production evidence bundle is missing or invalid"
                )
            payload = _read_bounded_json(
                evidence_path,
                maximum_bytes=MAX_EVIDENCE_BYTES,
                label="LFM production evidence bundle",
                expected_sha256=evidence_hash,
            )
            validate_release_approval_payload(
                payload,
                expected_binding={
                    "repository_id": self.LATE_INTERACTION_APPROVED_REPOSITORY_ID,
                    "build_sha": build_sha,
                    "source_digest": source_digest,
                    "model_revision": model_revision,
                    "index_revision": expected_index,
                    "lineage_id": lineage_id,
                    "identity_digest": identity_digest,
                    "document_count": (
                        self.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT
                    ),
                },
                reports_root=report_root,
            )
        return self


settings = Settings()
