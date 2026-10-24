"""Worker and indexing fields: queues, leases, maintenance, telemetry cadence."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import field_validator


class SettingsWorker:
    WORKER_REDIS_PREFIX: str = "brain:worker"

    # Minimum seconds between automatic reindexes of one repo triggered by stale_blocked context.
    AUTO_REINDEX_MIN_INTERVAL_S: int = 3600

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

    INDEX_LLM_SUMMARIES_MODE: Literal["lazy", "inline", "off"] = "lazy"

    INDEX_LLM_SUMMARIES_BACKGROUND: bool = False

    INDEX_SUMMARY_ENRICH_MAX_FILES: int = 1000

    INDEX_EMBEDDING_BATCH_SIZE: int = 8

    INDEX_GRAPH_BATCH_SIZE: int = 256

    INDEX_PROGRESS_INTERVAL_SECONDS: float = 10.0

    INDEX_REPAIR_CHUNK_LIMIT: int = 500

    HARNESS_STALE_REAPER_ENABLED: bool = True

    HARNESS_STALE_REAPER_BATCH_SIZE: int = 500

    # The daily health_check deletes mcp_tool_calls rows older than this; 0 keeps them all.
    MCP_TOOL_CALLS_RETENTION_DAYS: int = 30

    # Proactive insight loop. Manual job/API stays available; this gate is for
    # scheduled automation and tells operators whether the loop should run itself.
    PROACTIVE_INSIGHTS_ENABLED: bool = False

    PROACTIVE_INSIGHTS_LLM_ENABLED: bool = False

    PROACTIVE_INSIGHTS_MAX_SNAPSHOT_CHARS: int = 16000

    PROACTIVE_INSIGHTS_TIMEOUT_S: float = 75.0

    PROACTIVE_INSIGHTS_LLM_CACHE_TTL_SECONDS: int = 0

    # Closed-loop self diagnosis. Deterministic probes always run first; the LLM
    # may only synthesize evidence already present in that snapshot.
    SELF_DIAGNOSIS_ENABLED: bool = False

    SELF_DIAGNOSIS_USE_LLM: bool = True

    SELF_DIAGNOSIS_JOB_MAX_ATTEMPTS: int = 3

    SELF_DIAGNOSIS_RESULT_TTL_SECONDS: int = 7 * 24 * 60 * 60

    SELF_DIAGNOSIS_MIN_AVG_RECALL: float = 0.70

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

    @field_validator("WORKER_POOL_NAME")
    @classmethod
    def validate_worker_pool_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"fast", "maintenance", "deep"}:
            raise ValueError("WORKER_POOL_NAME must be fast, maintenance, or deep")
        return normalized
