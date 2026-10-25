"""Late-interaction (ColBERT) fields incl. remote/deep-lane knobs."""
from __future__ import annotations

from typing import Optional
from pydantic import field_validator


class SettingsLate:
    # Optional ColBERT late-interaction channel.  Every gate is false/zero by
    # default: the NVIDIA dense path remains authoritative until a measured
    # paired evaluation demonstrates retrieval quality and runtime parity.
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

    # Blank = the indexed repository (TARGET_REPO_PATH); see _default_repo_paths.
    NIGHTLY_DEEP_MAINTENANCE_REPO_PATH: str = ""

    NIGHTLY_DEEP_MAINTENANCE_NOTIFY: bool = True

    NIGHTLY_DEEP_MAINTENANCE_QUALITY_QUERIES: int = 3

    # Test/migration escape hatch only. Production requests must bind every
    # document/candidate to a SHA-256 content hash.
    LATE_INTERACTION_REMOTE_ALLOW_EMPTY_CONTENT_HASH: bool = False

    @field_validator("LATE_INTERACTION_CANARY_PERCENT")
    @classmethod
    def validate_late_interaction_canary_percent(cls, value: float) -> float:
        if not 0.0 <= value <= 100.0:
            raise ValueError("LATE_INTERACTION_CANARY_PERCENT must be between 0 and 100")
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
