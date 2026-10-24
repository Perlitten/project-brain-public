"""Assembled Settings class: domain field mixins plus cross-domain model validators."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse
from pydantic import model_validator
from pydantic_settings import SettingsConfigDict
from brain.release_gate.approval import MAX_EVIDENCE_BYTES, _read_bounded_json, validate_release_approval_payload
from brain.release_identity import read_baked_source_manifest
from ._core import SettingsCore
from ._infra import SettingsInfra
from ._worker import SettingsWorker
from ._team import SettingsTeam
from ._late import SettingsLate
from ._retrieval import SettingsRetrieval
from ._llm import SettingsLlm
from pydantic_settings import BaseSettings

from ._env import _env_files
from ._agent import SettingsAgent
from ._memory import SettingsMemory


class Settings(SettingsCore, SettingsInfra, SettingsWorker, SettingsTeam, SettingsLate, SettingsRetrieval, SettingsLlm, SettingsAgent, SettingsMemory, BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_files(), env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def validate_indexing_pool_capacity(self):
        if self.POSTGRES_POOL_SIZE + self.POSTGRES_MAX_OVERFLOW < 2:
            raise ValueError("Repository indexing locks require at least two PostgreSQL connections")
        return self

    @model_validator(mode="after")
    def _lite_defaults(self):
        if self.BRAIN_TEAM_MODE and self.BRAIN_STORAGE_MODE == "lite":
            raise ValueError("Strict team mode (BRAIN_TEAM_MODE) requires full storage; Lite is always a team of one")
        if self.BRAIN_STORAGE_MODE == "lite":
            if "DEFAULT_EMBEDDING_PROVIDER" not in self.model_fields_set:
                self.DEFAULT_EMBEDDING_PROVIDER = "local"
            if "DEFAULT_LLM_PROVIDER" not in self.model_fields_set:
                self.DEFAULT_LLM_PROVIDER = "disabled"
            if "BRAIN_AGENT_CONTEXT_V2_MODE" not in self.model_fields_set:
                self.BRAIN_AGENT_CONTEXT_V2_MODE = "on"
        return self

    @model_validator(mode="after")
    def _reject_default_secrets_in_production(self):
        """The dev defaults below are fine locally, but must never reach a
        production deploy. Fail fast at startup if they slip through."""
        if self.ENVIRONMENT.lower() == "production":
            insecure = {
                "POSTGRES_PASSWORD": "postgres_password",
                "NEO4J_PASSWORD": "neo4j_password",
                # Placeholders shipped in .env.example: anyone who read the
                # README knows them, so they are not secrets.
                "PROJECT_BRAIN_API_KEY": "change-me-in-production",
                "PROJECT_BRAIN_WEBHOOK_TOKEN": "change-me-to-a-random-64-character-token",
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
        return self

    @model_validator(mode="after")
    def _validate_oidc(self):
        if not self.BRAIN_OIDC_ENABLED:
            return self
        if not (self.BRAIN_OIDC_ISSUER or "").strip():
            raise ValueError("BRAIN_OIDC_ENABLED requires BRAIN_OIDC_ISSUER")
        if not (self.BRAIN_OIDC_CLIENT_ID or "").strip():
            raise ValueError("BRAIN_OIDC_ENABLED requires BRAIN_OIDC_CLIENT_ID")
        redirect = (self.BRAIN_OIDC_REDIRECT_URI or "").strip()
        if not redirect and (self.BRAIN_WEB_URL or "").strip():
            redirect = self.BRAIN_WEB_URL.strip().rstrip("/") + "/auth/oidc/callback"
        if not redirect:
            raise ValueError("BRAIN_OIDC_ENABLED requires BRAIN_OIDC_REDIRECT_URI or BRAIN_WEB_URL")
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        self.BRAIN_OIDC_ISSUER = self.BRAIN_OIDC_ISSUER.strip().rstrip("/")
        for name, value in (("BRAIN_OIDC_ISSUER", self.BRAIN_OIDC_ISSUER), ("BRAIN_OIDC_REDIRECT_URI", redirect)):
            parsed = urlparse(value)
            if not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{name} must be an absolute URL without credentials")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname.lower() in loopback_hosts):
                raise ValueError(f"{name} must use https (http is allowed only for loopback hosts)")
        self.BRAIN_OIDC_REDIRECT_URI = redirect
        if "openid" not in self.BRAIN_OIDC_SCOPES.split():
            raise ValueError("BRAIN_OIDC_SCOPES must include openid")
        return self

    @model_validator(mode="after")
    def _default_repo_paths(self):
        # Scheduled maintenance and the merge webhook act on the repository
        # Brain indexes unless told otherwise. A hard-coded container path here
        # made every scheduled job fail on a local (venv) install.
        target = (self.TARGET_REPO_PATH or ".").strip() or "."
        if not self.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH.strip():
            self.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH = target
        if not self.PROJECT_BRAIN_GIT_REPO_PATH.strip():
            self.PROJECT_BRAIN_GIT_REPO_PATH = target
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
