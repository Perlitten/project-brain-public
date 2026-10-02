"""Typed fail-open client for the co-located late-interaction GPU service.

Only source text crosses the write API. Token matrices stay inside the GPU
service and its local-volume SQLite store.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from brain.config.settings import settings


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHARED_CLIENT: RemoteLateInteractionClient | DisabledLateInteractionClient | None = None
_RELEASE_VALIDATION_ATTEMPTS = 4
_RELEASE_VALIDATION_INITIAL_BACKOFF_S = 0.5
_RELEASE_VALIDATION_MAX_BACKOFF_S = 2.0


class LateInteractionReleaseValidationError(RuntimeError):
    """The configured active release does not match its approved identity."""


class LateInteractionReleaseUnavailableError(LateInteractionReleaseValidationError):
    """The approved sidecar could not be reached after bounded retries."""


@dataclass(frozen=True)
class LateInteractionCandidate:
    chunk_id: int
    path: str
    content_hash: str = ""


@dataclass(frozen=True)
class LateInteractionDocument:
    chunk_id: int
    path: str
    content_hash: str
    text: str


@dataclass(frozen=True)
class LateInteractionScore:
    chunk_id: int
    path: str
    score: float


@dataclass(frozen=True)
class LateInteractionResult:
    status: str
    scores: tuple[LateInteractionScore, ...] = ()
    coverage: float = 0.0
    latency_ms: float = 0.0
    model_revision: str = ""
    index_revision: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LateInteractionMutationResult:
    status: str
    accepted: int = 0
    unchanged: int = 0
    deleted: int = 0
    model_revision: str = ""
    index_revision: str = ""
    latency_ms: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class LateInteractionIndexStatus:
    status: str
    repository_id: int
    model_revision: str = ""
    index_revision: str = ""
    lineage_id: str = ""
    identity_digest: str = ""
    document_count: int = 0
    minimum_document_count: int = 0
    bytes: int = 0
    last_update: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class LateInteractionIndexEntry:
    chunk_id: int
    path: str
    content_hash: str


@dataclass(frozen=True)
class LateInteractionIndexPage:
    status: str
    repository_id: int
    entries: tuple[LateInteractionIndexEntry, ...] = ()
    model_revision: str = ""
    index_revision: str = ""
    next_after_chunk_id: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class LateInteractionServiceStatus:
    status: str
    repository_id: int | None = None
    model_revision: str = ""
    index_revision: str = ""
    lineage_id: str = ""
    identity_digest: str = ""
    document_count: int = 0
    minimum_document_count: int = 0
    reason: str = ""


def _failure_reason(exc: BaseException) -> str:
    """Return telemetry-safe failure data without request bodies or URLs."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return type(exc).__name__
    return type(exc).__name__


def _transient_release_status(reason: str) -> bool:
    if reason in {
        "identity_refresh_pending",
        "timeout",
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "WriteError",
        "WriteTimeout",
    }:
        return True
    if reason.startswith("http_") and reason[5:].isdigit():
        return int(reason[5:]) in {408, 425, 429, 500, 502, 503, 504}
    return False


def _revision_number(value: str) -> int | None:
    if not value.startswith("r") or not value[1:].isdigit():
        return None
    return int(value[1:])


def _revision_mismatch(
    payload: dict[str, Any],
    model_revision: str,
    index_revision: str | None,
    *,
    allow_index_advance: bool = False,
) -> str:
    returned_model = str(payload.get("model_revision") or "")
    returned_index = str(payload.get("index_revision") or "")
    if returned_model != model_revision:
        return "model_revision_mismatch"
    if index_revision and returned_index != index_revision:
        expected_number = _revision_number(index_revision)
        returned_number = _revision_number(returned_index)
        if not (
            allow_index_advance
            and expected_number is not None
            and returned_number is not None
            and returned_number >= expected_number
        ):
            return "index_revision_mismatch"
    return ""


class DisabledLateInteractionClient:
    """Stable no-op used while the remote feature flag is disabled."""

    async def rerank(
        self,
        query: str,
        repository_id: int,
        candidates: Iterable[LateInteractionCandidate],
    ) -> LateInteractionResult:
        del query, repository_id, candidates
        return LateInteractionResult(status="disabled", reason="remote_disabled")

    async def upsert_documents(
        self,
        repository_id: int,
        documents: Iterable[LateInteractionDocument],
    ) -> LateInteractionMutationResult:
        del repository_id, documents
        return LateInteractionMutationResult(status="disabled", reason="remote_disabled")

    async def delete_documents(
        self,
        repository_id: int,
        chunk_ids: Iterable[int],
    ) -> LateInteractionMutationResult:
        del repository_id, chunk_ids
        return LateInteractionMutationResult(status="disabled", reason="remote_disabled")

    async def status(self, repository_id: int) -> LateInteractionIndexStatus:
        return LateInteractionIndexStatus(
            status="disabled",
            repository_id=repository_id,
            reason="remote_disabled",
        )

    async def finalize_index(self, repository_id: int) -> LateInteractionIndexStatus:
        return LateInteractionIndexStatus(
            status="disabled",
            repository_id=repository_id,
            reason="remote_disabled",
        )

    async def list_documents(
        self,
        repository_id: int,
        *,
        after_chunk_id: int = 0,
        limit: int = 1000,
    ) -> LateInteractionIndexPage:
        del after_chunk_id, limit
        return LateInteractionIndexPage(
            status="disabled",
            repository_id=repository_id,
            reason="remote_disabled",
        )

    async def ready(self) -> LateInteractionServiceStatus:
        return LateInteractionServiceStatus(
            status="disabled",
            reason="remote_disabled",
        )

    async def aclose(self) -> None:
        return None


class RemoteLateInteractionClient:
    """Authenticated bounded HTTP client.

    All failures are converted to typed fail-open results. No exception message
    is returned because httpx errors can include URLs or request details.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        model_revision: str,
        expected_index_revision: str | None = None,
        allow_index_revision_advance: bool = False,
        approved_repository_id: int | None = None,
        expected_lineage_id: str | None = None,
        expected_identity_digest: str | None = None,
        expected_document_count: int | None = None,
        minimum_document_count: int = 0,
        timeout_s: float = 2.0,
        mutation_timeout_s: float | None = None,
        max_candidates: int = 500,
        max_documents: int = 64,
        max_payload_bytes: int = 2_000_000,
        allow_empty_content_hash: bool = False,
        client: httpx.AsyncClient | None = None,
    ):
        if not token:
            raise ValueError("late-interaction remote token is required")
        if expected_identity_digest is not None and _SHA256_RE.fullmatch(expected_identity_digest) is None:
            raise ValueError("expected_identity_digest must be a SHA-256 digest")
        if expected_document_count is not None and expected_document_count < 0:
            raise ValueError("expected_document_count cannot be negative")
        if minimum_document_count < 0:
            raise ValueError("minimum_document_count cannot be negative")
        if expected_document_count is not None and expected_document_count < minimum_document_count:
            raise ValueError("expected_document_count cannot be below minimum_document_count")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model_revision = model_revision
        self.expected_index_revision = expected_index_revision
        self._current_exact_index_revision = expected_index_revision
        self.allow_index_revision_advance = allow_index_revision_advance
        self.approved_repository_id = approved_repository_id
        self.expected_lineage_id = expected_lineage_id
        self.expected_identity_digest = (
            expected_identity_digest.lower() if expected_identity_digest is not None else None
        )
        self.expected_document_count = expected_document_count
        self.minimum_document_count = minimum_document_count
        self.timeout_s = timeout_s
        self.mutation_timeout_s = mutation_timeout_s or timeout_s
        self.max_candidates = max_candidates
        self.max_documents = max_documents
        self.max_payload_bytes = max_payload_bytes
        self.allow_empty_content_hash = allow_empty_content_hash
        self._client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _active_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._owned_client is None:
            async with self._client_lock:
                if self._owned_client is None:
                    self._owned_client = httpx.AsyncClient(
                        timeout=self.timeout_s,
                        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                    )
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    def _valid_hash(self, value: str) -> bool:
        return bool(_SHA256_RE.fullmatch(value)) or (self.allow_empty_content_hash and not value)

    def _bounded_payload(self, payload: dict[str, Any]) -> bytes:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_payload_bytes:
            raise ValueError("payload_too_large")
        return encoded

    def _revision_preconditions(self) -> dict[str, str | None]:
        if self.allow_index_revision_advance:
            return {
                "index_revision": None,
                "minimum_index_revision": self.expected_index_revision,
            }
        return {
            "index_revision": self._current_exact_index_revision,
            "minimum_index_revision": None,
        }

    def _response_index_revision(self) -> str | None:
        if self.allow_index_revision_advance:
            return self.expected_index_revision
        return self._current_exact_index_revision

    def _repository_is_approved(self, repository_id: int) -> bool:
        return self.approved_repository_id is None or repository_id == self.approved_repository_id

    def _inventory_mismatch(
        self,
        payload: dict[str, Any],
        repository_id: int,
    ) -> str:
        raw_repository_id = payload.get("repository_id")
        if raw_repository_id is None:
            return "repository_identity_missing"
        try:
            returned_repository_id = int(raw_repository_id)
        except (TypeError, ValueError):
            return "repository_identity_missing"
        if returned_repository_id != repository_id:
            return "repository_identity_mismatch"
        lineage_id = str(payload.get("lineage_id") or "")
        identity_digest = str(payload.get("identity_digest") or "").lower()
        document_count = int(payload.get("document_count") or 0)
        if self.expected_lineage_id is not None and lineage_id != self.expected_lineage_id:
            return "lineage_id_mismatch"
        if self.expected_identity_digest is not None and identity_digest != self.expected_identity_digest:
            return "identity_digest_mismatch"
        if self.expected_document_count is not None and document_count != self.expected_document_count:
            return "document_count_mismatch"
        if document_count < self.minimum_document_count:
            return "document_count_below_minimum"
        return ""

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        from brain.late_interaction.metrics import increment, observe

        started = time.perf_counter()
        increment("provider_requests")
        try:
            client = await self._active_client()
            kwargs: dict[str, Any] = {
                "headers": {
                    "X-Late-Interaction-Token": self.token,
                    "Accept": "application/json",
                },
                "timeout": timeout_s or self.timeout_s,
            }
            if payload is not None:
                kwargs["content"] = self._bounded_payload(payload)
                kwargs["headers"]["Content-Type"] = "application/json"
            response = await client.request(method, f"{self.base_url}{path}", **kwargs)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("invalid_response")
            return value
        except asyncio.CancelledError:
            increment("provider_failures")
            raise
        except Exception:
            increment("provider_failures")
            raise
        finally:
            observe(
                "provider_latency_ms_total",
                (time.perf_counter() - started) * 1000,
            )

    async def rerank(
        self,
        query: str,
        repository_id: int,
        candidates: Iterable[LateInteractionCandidate],
    ) -> LateInteractionResult:
        started = time.perf_counter()
        if not self._repository_is_approved(repository_id):
            return LateInteractionResult(
                status="failed_open",
                model_revision=self.model_revision,
                reason="repository_not_approved",
            )
        items = list(candidates)[: self.max_candidates]
        invalid = next((item for item in items if not self._valid_hash(item.content_hash)), None)
        if invalid is not None:
            return LateInteractionResult(
                status="failed_open",
                latency_ms=(time.perf_counter() - started) * 1000,
                model_revision=self.model_revision,
                reason="content_hash_required",
            )
        payload = {
            "query": query,
            "repository_id": repository_id,
            "model_revision": self.model_revision,
            **self._revision_preconditions(),
            "candidates": [
                {
                    "chunk_id": item.chunk_id,
                    "path": item.path,
                    "content_hash": item.content_hash.lower(),
                }
                for item in items
            ],
        }
        try:
            value = await self._request("POST", "/v1/rerank", payload)
            mismatch = _revision_mismatch(
                value,
                self.model_revision,
                self._response_index_revision(),
                allow_index_advance=self.allow_index_revision_advance,
            )
            if mismatch:
                return LateInteractionResult(
                    status="revision_mismatch",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    reason=mismatch,
                )
            inventory_mismatch = self._inventory_mismatch(value, repository_id)
            if inventory_mismatch:
                return LateInteractionResult(
                    status="inventory_mismatch",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    reason=inventory_mismatch,
                )
            scores = tuple(
                LateInteractionScore(
                    chunk_id=int(item["chunk_id"]),
                    path=str(item["path"]),
                    score=float(item["score"]),
                )
                for item in value.get("scores", [])
            )
            return LateInteractionResult(
                status=str(value.get("status") or "failed_open"),
                scores=scores,
                coverage=float(value.get("coverage") or 0.0),
                latency_ms=float(value.get("latency_ms") or ((time.perf_counter() - started) * 1000)),
                model_revision=str(value.get("model_revision") or ""),
                index_revision=str(value.get("index_revision") or ""),
                reason=str(value.get("reason") or ""),
            )
        except Exception as exc:
            return LateInteractionResult(
                status="failed_open",
                latency_ms=(time.perf_counter() - started) * 1000,
                model_revision=self.model_revision,
                reason=_failure_reason(exc),
            )

    async def upsert_documents(
        self,
        repository_id: int,
        documents: Iterable[LateInteractionDocument],
    ) -> LateInteractionMutationResult:
        started = time.perf_counter()
        if not self._repository_is_approved(repository_id):
            return LateInteractionMutationResult(
                status="failed_open",
                model_revision=self.model_revision,
                reason="repository_not_approved",
            )
        items = list(documents)
        if len(items) > self.max_documents:
            return LateInteractionMutationResult(
                status="failed_open",
                model_revision=self.model_revision,
                reason="document_batch_too_large",
            )
        if any(not self._valid_hash(item.content_hash) for item in items):
            return LateInteractionMutationResult(
                status="failed_open",
                model_revision=self.model_revision,
                reason="content_hash_required",
            )
        payload = {
            "repository_id": repository_id,
            "model_revision": self.model_revision,
            **self._revision_preconditions(),
            "documents": [
                {
                    "chunk_id": item.chunk_id,
                    "path": item.path,
                    "content_hash": item.content_hash.lower(),
                    "text": item.text,
                }
                for item in items
            ],
        }
        try:
            value = await self._request(
                "POST",
                "/v1/index/upsert",
                payload,
                timeout_s=self.mutation_timeout_s,
            )
            return self._mutation_from_response(value, started)
        except Exception as exc:
            return LateInteractionMutationResult(
                status="failed_open",
                latency_ms=(time.perf_counter() - started) * 1000,
                model_revision=self.model_revision,
                reason=_failure_reason(exc),
            )

    async def delete_documents(
        self,
        repository_id: int,
        chunk_ids: Iterable[int],
    ) -> LateInteractionMutationResult:
        started = time.perf_counter()
        if not self._repository_is_approved(repository_id):
            return LateInteractionMutationResult(
                status="failed_open",
                model_revision=self.model_revision,
                reason="repository_not_approved",
            )
        payload = {
            "repository_id": repository_id,
            "model_revision": self.model_revision,
            **self._revision_preconditions(),
            "chunk_ids": list(chunk_ids),
        }
        try:
            value = await self._request(
                "POST",
                "/v1/index/delete",
                payload,
                timeout_s=self.mutation_timeout_s,
            )
            return self._mutation_from_response(value, started)
        except Exception as exc:
            return LateInteractionMutationResult(
                status="failed_open",
                latency_ms=(time.perf_counter() - started) * 1000,
                model_revision=self.model_revision,
                reason=_failure_reason(exc),
            )

    def _mutation_from_response(
        self,
        value: dict[str, Any],
        started: float,
    ) -> LateInteractionMutationResult:
        # The request revision is an optimistic-concurrency precondition; a
        # successful exact-mode mutation is expected to return a newer index
        # revision. In live/dual-write mode the approved revision is a
        # readiness floor, not a write CAS: recovery must be able to rebuild an
        # empty store from r0 while reads remain fail-closed below the floor.
        mutation_precondition = None if self.allow_index_revision_advance else self._current_exact_index_revision
        mismatch = _revision_mismatch(
            value,
            self.model_revision,
            mutation_precondition,
            allow_index_advance=True,
        )
        if mismatch:
            return LateInteractionMutationResult(
                status="revision_mismatch",
                model_revision=str(value.get("model_revision") or ""),
                index_revision=str(value.get("index_revision") or ""),
                latency_ms=(time.perf_counter() - started) * 1000,
                reason=mismatch,
            )
        result = LateInteractionMutationResult(
            status=str(value.get("status") or "failed_open"),
            accepted=int(value.get("accepted") or 0),
            unchanged=int(value.get("unchanged") or 0),
            deleted=int(value.get("deleted") or 0),
            model_revision=str(value.get("model_revision") or ""),
            index_revision=str(value.get("index_revision") or ""),
            latency_ms=float(value.get("latency_ms") or ((time.perf_counter() - started) * 1000)),
            reason=str(value.get("reason") or ""),
        )
        if (
            not self.allow_index_revision_advance
            and result.status in {"updated", "unchanged"}
            and result.index_revision
        ):
            self._current_exact_index_revision = result.index_revision
        return result

    async def status(self, repository_id: int) -> LateInteractionIndexStatus:
        if not self._repository_is_approved(repository_id):
            return LateInteractionIndexStatus(
                status="failed_open",
                repository_id=repository_id,
                model_revision=self.model_revision,
                reason="repository_not_approved",
            )
        try:
            value = await self._request(
                "GET",
                (f"/v1/index/status?repository_id={repository_id}&model_revision={self.model_revision}"),
            )
            mismatch = _revision_mismatch(
                value,
                self.model_revision,
                self._response_index_revision(),
                allow_index_advance=self.allow_index_revision_advance,
            )
            if mismatch:
                return LateInteractionIndexStatus(
                    status="revision_mismatch",
                    repository_id=repository_id,
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    lineage_id=str(value.get("lineage_id") or ""),
                    identity_digest=str(value.get("identity_digest") or ""),
                    document_count=int(value.get("document_count") or 0),
                    minimum_document_count=int(value.get("minimum_document_count") or 0),
                    reason=mismatch,
                )
            inventory_mismatch = self._inventory_mismatch(value, repository_id)
            if inventory_mismatch:
                return LateInteractionIndexStatus(
                    status="inventory_mismatch",
                    repository_id=repository_id,
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    lineage_id=str(value.get("lineage_id") or ""),
                    identity_digest=str(value.get("identity_digest") or ""),
                    document_count=int(value.get("document_count") or 0),
                    minimum_document_count=int(value.get("minimum_document_count") or 0),
                    bytes=int(value.get("bytes") or 0),
                    last_update=value.get("last_update"),
                    reason=inventory_mismatch,
                )
            return LateInteractionIndexStatus(
                status=str(value.get("status") or "failed_open"),
                repository_id=repository_id,
                model_revision=str(value.get("model_revision") or ""),
                index_revision=str(value.get("index_revision") or ""),
                lineage_id=str(value.get("lineage_id") or ""),
                identity_digest=str(value.get("identity_digest") or ""),
                document_count=int(value.get("document_count") or 0),
                minimum_document_count=int(value.get("minimum_document_count") or 0),
                bytes=int(value.get("bytes") or 0),
                last_update=value.get("last_update"),
                reason=str(value.get("reason") or ""),
            )
        except Exception as exc:
            return LateInteractionIndexStatus(
                status="failed_open",
                repository_id=repository_id,
                model_revision=self.model_revision,
                reason=_failure_reason(exc),
            )

    async def finalize_index(self, repository_id: int) -> LateInteractionIndexStatus:
        """Finalize one corpus digest after a complete mutation batch."""
        if not self._repository_is_approved(repository_id):
            return LateInteractionIndexStatus(
                status="failed_open",
                repository_id=repository_id,
                model_revision=self.model_revision,
                reason="repository_not_approved",
            )
        try:
            value = await self._request(
                "POST",
                "/v1/index/finalize",
                {
                    "repository_id": repository_id,
                    "model_revision": self.model_revision,
                },
                timeout_s=self.mutation_timeout_s,
            )
            mismatch = _revision_mismatch(
                value,
                self.model_revision,
                self._response_index_revision(),
                allow_index_advance=self.allow_index_revision_advance,
            )
            if mismatch:
                return LateInteractionIndexStatus(
                    status="revision_mismatch",
                    repository_id=repository_id,
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    lineage_id=str(value.get("lineage_id") or ""),
                    identity_digest=str(value.get("identity_digest") or ""),
                    document_count=int(value.get("document_count") or 0),
                    minimum_document_count=int(value.get("minimum_document_count") or 0),
                    reason=mismatch,
                )
            inventory_mismatch = self._inventory_mismatch(value, repository_id)
            if inventory_mismatch:
                return LateInteractionIndexStatus(
                    status="inventory_mismatch",
                    repository_id=repository_id,
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    lineage_id=str(value.get("lineage_id") or ""),
                    identity_digest=str(value.get("identity_digest") or ""),
                    document_count=int(value.get("document_count") or 0),
                    minimum_document_count=int(value.get("minimum_document_count") or 0),
                    bytes=int(value.get("bytes") or 0),
                    last_update=value.get("last_update"),
                    reason=inventory_mismatch,
                )
            return LateInteractionIndexStatus(
                status=str(value.get("status") or "failed_open"),
                repository_id=repository_id,
                model_revision=str(value.get("model_revision") or ""),
                index_revision=str(value.get("index_revision") or ""),
                lineage_id=str(value.get("lineage_id") or ""),
                identity_digest=str(value.get("identity_digest") or ""),
                document_count=int(value.get("document_count") or 0),
                minimum_document_count=int(value.get("minimum_document_count") or 0),
                bytes=int(value.get("bytes") or 0),
                last_update=value.get("last_update"),
                reason=str(value.get("reason") or ""),
            )
        except Exception as exc:
            return LateInteractionIndexStatus(
                status="failed_open",
                repository_id=repository_id,
                model_revision=self.model_revision,
                reason=_failure_reason(exc),
            )

    async def list_documents(
        self,
        repository_id: int,
        *,
        after_chunk_id: int = 0,
        limit: int = 1000,
    ) -> LateInteractionIndexPage:
        if not self._repository_is_approved(repository_id):
            return LateInteractionIndexPage(
                status="failed_open",
                repository_id=repository_id,
                model_revision=self.model_revision,
                reason="repository_not_approved",
            )
        bounded_limit = max(1, min(int(limit), 1000))
        try:
            value = await self._request(
                "GET",
                (
                    f"/v1/index/documents?repository_id={repository_id}"
                    f"&model_revision={self.model_revision}"
                    f"&after_chunk_id={max(0, int(after_chunk_id))}"
                    f"&limit={bounded_limit}"
                ),
            )
            mismatch = _revision_mismatch(
                value,
                self.model_revision,
                # Authenticated inventory is the repair plane. It must expose
                # r0 and other below-floor revisions so exact remote sync can
                # rebuild the store; serving endpoints keep the release floor.
                None,
            )
            if mismatch:
                return LateInteractionIndexPage(
                    status="revision_mismatch",
                    repository_id=repository_id,
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    reason=mismatch,
                )
            entries = tuple(
                LateInteractionIndexEntry(
                    chunk_id=int(item["chunk_id"]),
                    path=str(item["path"]),
                    content_hash=str(item["content_hash"]),
                )
                for item in value.get("documents", [])
            )
            next_after = value.get("next_after_chunk_id")
            return LateInteractionIndexPage(
                status=str(value.get("status") or "failed_open"),
                repository_id=repository_id,
                entries=entries,
                model_revision=str(value.get("model_revision") or ""),
                index_revision=str(value.get("index_revision") or ""),
                next_after_chunk_id=int(next_after) if next_after is not None else None,
                reason=str(value.get("reason") or ""),
            )
        except Exception as exc:
            return LateInteractionIndexPage(
                status="failed_open",
                repository_id=repository_id,
                model_revision=self.model_revision,
                reason=_failure_reason(exc),
            )

    async def ready(self) -> LateInteractionServiceStatus:
        try:
            value = await self._request("GET", "/ready")
            mismatch = _revision_mismatch(
                value,
                self.model_revision,
                self._response_index_revision() if self.approved_repository_id is not None else None,
                allow_index_advance=self.allow_index_revision_advance,
            )
            if mismatch:
                return LateInteractionServiceStatus(
                    status="revision_mismatch",
                    repository_id=value.get("repository_id"),
                    model_revision=str(value.get("model_revision") or ""),
                    index_revision=str(value.get("index_revision") or ""),
                    lineage_id=str(value.get("lineage_id") or ""),
                    identity_digest=str(value.get("identity_digest") or ""),
                    document_count=int(value.get("document_count") or 0),
                    minimum_document_count=int(value.get("minimum_document_count") or 0),
                    reason=mismatch,
                )
            if self.approved_repository_id is not None:
                inventory_mismatch = self._inventory_mismatch(
                    value,
                    self.approved_repository_id,
                )
                if inventory_mismatch:
                    return LateInteractionServiceStatus(
                        status="inventory_mismatch",
                        repository_id=value.get("repository_id"),
                        model_revision=str(value.get("model_revision") or ""),
                        index_revision=str(value.get("index_revision") or ""),
                        lineage_id=str(value.get("lineage_id") or ""),
                        identity_digest=str(value.get("identity_digest") or ""),
                        document_count=int(value.get("document_count") or 0),
                        minimum_document_count=int(value.get("minimum_document_count") or 0),
                        reason=inventory_mismatch,
                    )
            return LateInteractionServiceStatus(
                status=str(value.get("status") or "failed_open"),
                repository_id=value.get("repository_id"),
                model_revision=str(value.get("model_revision") or ""),
                index_revision=str(value.get("index_revision") or ""),
                lineage_id=str(value.get("lineage_id") or ""),
                identity_digest=str(value.get("identity_digest") or ""),
                document_count=int(value.get("document_count") or 0),
                minimum_document_count=int(value.get("minimum_document_count") or 0),
                reason=str(value.get("reason") or ""),
            )
        except Exception as exc:
            return LateInteractionServiceStatus(
                status="failed_open",
                model_revision=self.model_revision,
                reason=_failure_reason(exc),
            )


def get_late_interaction_client() -> RemoteLateInteractionClient | DisabledLateInteractionClient:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        if not settings.LATE_INTERACTION_REMOTE_ENABLED:
            _SHARED_CLIENT = DisabledLateInteractionClient()
        else:
            allow_index_advance = settings.LATE_INTERACTION_DUAL_WRITE_ENABLED
            approved_document_count = settings.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT
            _SHARED_CLIENT = RemoteLateInteractionClient(
                base_url=settings.LATE_INTERACTION_REMOTE_URL,
                token=settings.LATE_INTERACTION_REMOTE_TOKEN or "",
                model_revision=settings.LATE_INTERACTION_REMOTE_MODEL_REVISION,
                expected_index_revision=settings.LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION,
                allow_index_revision_advance=allow_index_advance,
                approved_repository_id=(settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID),
                expected_lineage_id=settings.LATE_INTERACTION_APPROVED_LINEAGE_ID,
                expected_identity_digest=(
                    None if allow_index_advance else settings.LATE_INTERACTION_APPROVED_IDENTITY_DIGEST
                ),
                expected_document_count=(None if allow_index_advance else approved_document_count),
                minimum_document_count=(int(approved_document_count or 0) if allow_index_advance else 0),
                timeout_s=settings.LATE_INTERACTION_REMOTE_TIMEOUT_S,
                mutation_timeout_s=settings.LATE_INTERACTION_REMOTE_MUTATION_TIMEOUT_S,
                max_candidates=settings.LATE_INTERACTION_REMOTE_MAX_CANDIDATES,
                max_documents=settings.LATE_INTERACTION_REMOTE_MAX_DOCUMENTS,
                max_payload_bytes=settings.LATE_INTERACTION_REMOTE_MAX_PAYLOAD_BYTES,
                allow_empty_content_hash=settings.LATE_INTERACTION_REMOTE_ALLOW_EMPTY_CONTENT_HASH,
            )
    return _SHARED_CLIENT


def build_maintenance_late_interaction_client(
    *,
    repository_id: int,
    index_revision: str | None = None,
    identity_digest: str | None = None,
    document_count: int | None = None,
) -> RemoteLateInteractionClient:
    """Build a job-scoped client for exact maintenance and deep retrieval.

    The shared client is intentionally bound to static production approval.
    Nightly maintenance must be able to advance an inert index without turning
    dual-write or online traffic on. An unbound maintenance client may mutate,
    but only for the configured repository/model/lineage. Once an exact sync
    proves the full inventory, callers build a second exact client from the
    returned revision/digest/count before scoring any user task.
    """

    if not settings.LATE_INTERACTION_REMOTE_ENABLED:
        raise ValueError("late-interaction remote service is disabled")
    approved_repository_id = settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID
    if approved_repository_id is None or repository_id != approved_repository_id:
        raise ValueError("repository is not approved for LFM maintenance")

    exact_values = (index_revision, identity_digest, document_count)
    exact = all(value is not None for value in exact_values)
    if any(value is not None for value in exact_values) and not exact:
        raise ValueError(
            "exact LFM client requires index revision, identity digest and "
            "document count together"
        )

    return RemoteLateInteractionClient(
        base_url=settings.LATE_INTERACTION_REMOTE_URL,
        token=settings.LATE_INTERACTION_REMOTE_TOKEN or "",
        model_revision=settings.LATE_INTERACTION_REMOTE_MODEL_REVISION,
        expected_index_revision=index_revision if exact else None,
        allow_index_revision_advance=not exact,
        approved_repository_id=repository_id,
        expected_lineage_id=settings.LATE_INTERACTION_APPROVED_LINEAGE_ID,
        expected_identity_digest=identity_digest if exact else None,
        expected_document_count=document_count if exact else None,
        minimum_document_count=0,
        timeout_s=settings.LATE_INTERACTION_DEEP_TIMEOUT_S,
        mutation_timeout_s=settings.LATE_INTERACTION_REMOTE_MUTATION_TIMEOUT_S,
        max_candidates=settings.LATE_INTERACTION_REMOTE_MAX_CANDIDATES,
        max_documents=settings.LATE_INTERACTION_REMOTE_MAX_DOCUMENTS,
        max_payload_bytes=settings.LATE_INTERACTION_REMOTE_MAX_PAYLOAD_BYTES,
        allow_empty_content_hash=settings.LATE_INTERACTION_REMOTE_ALLOW_EMPTY_CONTENT_HASH,
    )


def late_interaction_release_active() -> bool:
    return any(
        (
            settings.LATE_INTERACTION_ENABLED,
            settings.LATE_INTERACTION_DUAL_WRITE_ENABLED,
            settings.LATE_INTERACTION_SHADOW_ENABLED,
            settings.LATE_INTERACTION_RERANK_ENABLED,
            settings.LATE_INTERACTION_CANARY_PERCENT > 0,
        )
    )


async def validate_remote_late_interaction_release(
    *,
    max_attempts: int = _RELEASE_VALIDATION_ATTEMPTS,
    initial_backoff_s: float = _RELEASE_VALIDATION_INITIAL_BACKOFF_S,
) -> LateInteractionIndexStatus | None:
    """Verify the approved repository/model/index before serving active work."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if initial_backoff_s < 0:
        raise ValueError("initial_backoff_s cannot be negative")
    if not late_interaction_release_active():
        return None
    if not settings.LATE_INTERACTION_REMOTE_ENABLED:
        if settings.ENVIRONMENT.lower() == "production":
            raise LateInteractionReleaseValidationError(
                "active production LFM requires the authenticated remote service"
            )
        return None
    repository_id = settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID
    if repository_id is None:
        raise LateInteractionReleaseValidationError("active LFM release is missing its remote repository binding")
    remote = get_late_interaction_client()
    for attempt in range(max_attempts):
        try:
            status = await remote.status(repository_id)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            reason = _failure_reason(exc)
        else:
            if status.status == "ready":
                return status
            reason = status.reason or status.status
            transient = status.status in {"failed_open", "not_ready"} and _transient_release_status(reason)
            if not transient:
                raise LateInteractionReleaseValidationError(
                    f"active LFM release failed remote index validation: {reason}"
                )
        if attempt + 1 >= max_attempts:
            raise LateInteractionReleaseUnavailableError(
                f"active LFM release failed remote index validation after {max_attempts} attempts: {reason}"
            )
        delay = min(
            initial_backoff_s * (2**attempt),
            _RELEASE_VALIDATION_MAX_BACKOFF_S,
        )
        if delay:
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def close_late_interaction_client() -> None:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is not None:
        await _SHARED_CLIENT.aclose()
        _SHARED_CLIENT = None
