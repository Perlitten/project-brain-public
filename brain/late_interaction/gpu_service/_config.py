"""Service configuration from environment plus the pluggable encoder protocol."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import urlparse
import numpy as np


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceConfig:
    database_path: str = "./data/late-interaction.sqlite3"
    token: str = ""
    environment: str = "production"
    model_name: str = "LiquidAI/LFM2.5-ColBERT-350M"
    model_revision: str = "59633c2e31717b3502343ff566bee9fda3261943"
    license_acknowledged: bool = False
    dimension: int = 128
    query_max_tokens: int = 32
    document_max_tokens: int = 512
    max_candidates: int = 500
    max_documents: int = 64
    max_text_chars: int = 64_000
    encoder_batch_size: int = 16
    score_batch_size: int = 32
    max_concurrency: int = 2
    allow_empty_content_hash: bool = False
    encoder_backend: str = "pylate"
    llama_cpp_url: str = "http://127.0.0.1:8089"
    llama_cpp_timeout_s: float = 60.0
    llama_cpp_max_concurrency: int = 1
    llama_cpp_expected_model_alias: str = ""
    llama_cpp_expected_model_ftype: str = ""
    approved_repository_id: int | None = None
    minimum_index_revision: str | None = None
    minimum_document_count: int = 0

    @classmethod
    def from_env(cls) -> ServiceConfig:
        return cls(
            database_path=os.getenv(
                "LATE_INTERACTION_SERVICE_DATABASE_PATH",
                "./data/late-interaction.sqlite3",
            ),
            token=os.getenv("LATE_INTERACTION_SERVICE_TOKEN", ""),
            environment=os.getenv("LATE_INTERACTION_SERVICE_ENV", "production"),
            model_name=os.getenv(
                "LATE_INTERACTION_SERVICE_MODEL",
                "LiquidAI/LFM2.5-ColBERT-350M",
            ),
            model_revision=os.getenv(
                "LATE_INTERACTION_SERVICE_MODEL_REVISION",
                "59633c2e31717b3502343ff566bee9fda3261943",
            ),
            license_acknowledged=_env_bool("LFM_LICENSE_ACKNOWLEDGED"),
            dimension=int(os.getenv("LATE_INTERACTION_SERVICE_DIMENSION", "128")),
            query_max_tokens=int(os.getenv("LATE_INTERACTION_SERVICE_QUERY_MAX_TOKENS", "32")),
            document_max_tokens=int(os.getenv("LATE_INTERACTION_SERVICE_DOCUMENT_MAX_TOKENS", "512")),
            max_candidates=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_CANDIDATES", "500")),
            max_documents=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_DOCUMENTS", "64")),
            max_text_chars=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_TEXT_CHARS", "64000")),
            encoder_batch_size=int(os.getenv("LATE_INTERACTION_SERVICE_ENCODER_BATCH_SIZE", "16")),
            score_batch_size=int(os.getenv("LATE_INTERACTION_SERVICE_SCORE_BATCH_SIZE", "32")),
            max_concurrency=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_CONCURRENCY", "2")),
            allow_empty_content_hash=_env_bool("LATE_INTERACTION_SERVICE_ALLOW_EMPTY_CONTENT_HASH"),
            encoder_backend=(
                os.getenv("LATE_INTERACTION_SERVICE_ENCODER_BACKEND", "pylate").strip().lower() or "pylate"
            ),
            llama_cpp_url=os.getenv(
                "LATE_INTERACTION_SERVICE_LLAMA_CPP_URL",
                "http://127.0.0.1:8089",
            ).rstrip("/"),
            llama_cpp_timeout_s=float(os.getenv("LATE_INTERACTION_SERVICE_LLAMA_CPP_TIMEOUT_S", "60")),
            llama_cpp_max_concurrency=int(
                os.getenv(
                    "LATE_INTERACTION_SERVICE_LLAMA_CPP_MAX_CONCURRENCY",
                    "1",
                )
            ),
            llama_cpp_expected_model_alias=os.getenv(
                "LATE_INTERACTION_SERVICE_LLAMA_CPP_EXPECTED_MODEL_ALIAS",
                "",
            ),
            llama_cpp_expected_model_ftype=os.getenv(
                "LATE_INTERACTION_SERVICE_LLAMA_CPP_EXPECTED_MODEL_FTYPE",
                "",
            ),
            approved_repository_id=_env_optional_int("LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID"),
            minimum_index_revision=(
                os.getenv(
                    "LATE_INTERACTION_SERVICE_MINIMUM_INDEX_REVISION",
                    "",
                ).strip()
                or None
            ),
            minimum_document_count=int(
                os.getenv(
                    "LATE_INTERACTION_SERVICE_MINIMUM_DOCUMENT_COUNT",
                    "0",
                )
            ),
        )

    def validate_startup(self) -> None:
        if not self.token:
            raise RuntimeError("LATE_INTERACTION_SERVICE_TOKEN is required")
        if self.environment.lower() != "test" and not self.license_acknowledged:
            raise RuntimeError("non-test late-interaction service requires LFM_LICENSE_ACKNOWLEDGED=true")
        positive = {
            "dimension": self.dimension,
            "query_max_tokens": self.query_max_tokens,
            "document_max_tokens": self.document_max_tokens,
            "max_candidates": self.max_candidates,
            "max_documents": self.max_documents,
            "max_text_chars": self.max_text_chars,
            "encoder_batch_size": self.encoder_batch_size,
            "score_batch_size": self.score_batch_size,
            "max_concurrency": self.max_concurrency,
            "llama_cpp_max_concurrency": self.llama_cpp_max_concurrency,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise RuntimeError("positive late-interaction service settings required: " + ", ".join(invalid))
        if self.max_candidates > 500:
            raise RuntimeError("LATE_INTERACTION_SERVICE_MAX_CANDIDATES cannot exceed 500")
        backend = self.encoder_backend.strip().lower()
        if backend not in {"pylate", "llama_cpp"}:
            raise RuntimeError("LATE_INTERACTION_SERVICE_ENCODER_BACKEND must be pylate or llama_cpp")
        if self.llama_cpp_timeout_s <= 0:
            raise RuntimeError("LATE_INTERACTION_SERVICE_LLAMA_CPP_TIMEOUT_S must be positive")
        if self.approved_repository_id is not None and self.approved_repository_id < 1:
            raise RuntimeError("LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID must be positive")
        if self.minimum_document_count < 0:
            raise RuntimeError("LATE_INTERACTION_SERVICE_MINIMUM_DOCUMENT_COUNT cannot be negative")
        if self.minimum_index_revision is not None and _revision_number(self.minimum_index_revision) is None:
            raise RuntimeError("LATE_INTERACTION_SERVICE_MINIMUM_INDEX_REVISION must use the rN format")
        if self.approved_repository_id is None and (
            self.minimum_index_revision is not None or self.minimum_document_count > 0
        ):
            raise RuntimeError("service index floors require LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID")
        if backend == "llama_cpp":
            parsed = urlparse(self.llama_cpp_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise RuntimeError("LATE_INTERACTION_SERVICE_LLAMA_CPP_URL must be an absolute HTTP(S) URL")
            if parsed.username or parsed.password:
                raise RuntimeError("LATE_INTERACTION_SERVICE_LLAMA_CPP_URL must not contain credentials")
            if self.environment.lower() != "test" and not self.llama_cpp_expected_model_alias:
                raise RuntimeError("production llama_cpp backend requires an expected model alias")
            if self.environment.lower() != "test" and not self.llama_cpp_expected_model_ftype:
                raise RuntimeError("production llama_cpp backend requires an expected model type")


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _create_encoder(config: ServiceConfig) -> LateInteractionEncoder:
    backend = config.encoder_backend.strip().lower()
    if backend == "pylate":
        from brain.late_interaction.pylate_encoder import PyLateEncoder

        return PyLateEncoder(
            model_name=config.model_name,
            model_revision=config.model_revision,
            dimension=config.dimension,
            batch_size=config.encoder_batch_size,
            query_max_tokens=config.query_max_tokens,
            document_max_tokens=config.document_max_tokens,
        )
    if backend == "llama_cpp":
        from brain.late_interaction.llama_cpp_encoder import LlamaCppEncoder

        return LlamaCppEncoder(
            base_url=config.llama_cpp_url,
            model_name=config.model_name,
            model_revision=config.model_revision,
            dimension=config.dimension,
            query_max_tokens=config.query_max_tokens,
            document_max_tokens=config.document_max_tokens,
            timeout_s=config.llama_cpp_timeout_s,
            max_concurrency=config.llama_cpp_max_concurrency,
            expected_model_alias=config.llama_cpp_expected_model_alias,
            expected_model_ftype=config.llama_cpp_expected_model_ftype,
        )
    raise RuntimeError("LATE_INTERACTION_SERVICE_ENCODER_BACKEND must be pylate or llama_cpp")


class LateInteractionEncoder(Protocol):
    model_revision: str
    dimension: int

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]: ...

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]: ...


def _revision_number(value: str) -> int | None:
    if not value.startswith("r") or not value[1:].isdigit():
        return None
    return int(value[1:])
