"""FastAPI application factory: auth, mutation and rerank endpoints."""
from __future__ import annotations

import asyncio
import hmac
import time
from typing import Annotated, Any, Sequence
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from ._config import _revision_number, LateInteractionEncoder, ServiceConfig, _SHA256_RE, _create_encoder, logger
from ._models import (CandidateInput, DeleteRequest, DocumentInput, FinalizeRequest, PruneRequest, RerankRequest, UpsertRequest)
from ._store import AsyncRevisionLock, IndexInventory, IndexReleaseIdentity, SQLiteMatrixStore, StoredMatrix


def _valid_hash(value: str, *, allow_empty: bool) -> bool:
    return bool(_SHA256_RE.fullmatch(value)) or (allow_empty and not value)


def _revision_mismatch(
    *,
    requested_model: str,
    configured_model: str,
    requested_index: str | None,
    minimum_index: str | None,
    current_index: str,
    enforced_minimum_index: str | None = None,
) -> str:
    if requested_model != configured_model:
        return "model_revision_mismatch"
    if requested_index and requested_index != current_index:
        return "index_revision_mismatch"
    for floor in (enforced_minimum_index, minimum_index):
        if not floor:
            continue
        requested_floor = _revision_number(floor)
        current_revision = _revision_number(current_index)
        if requested_floor is None or current_revision is None or current_revision < requested_floor:
            return "index_revision_below_approved_floor"
    return ""



def _batched_maxsim(
    query: np.ndarray,
    documents: Sequence[np.ndarray],
    *,
    batch_size: int,
) -> list[float]:
    q = np.asarray(query, dtype=np.float32)
    if q.ndim != 2 or q.shape[0] == 0:
        raise ValueError("query matrix must be non-empty and two-dimensional")
    scores: list[float] = []
    for offset in range(0, len(documents), batch_size):
        batch = [np.asarray(value, dtype=np.float32) for value in documents[offset : offset + batch_size]]
        max_tokens = max(value.shape[0] for value in batch)
        padded = np.zeros((len(batch), max_tokens, q.shape[1]), dtype=np.float32)
        mask = np.zeros((len(batch), max_tokens), dtype=bool)
        for index, matrix in enumerate(batch):
            if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] != q.shape[1]:
                raise ValueError("document/query matrix shape mismatch")
            padded[index, : matrix.shape[0], :] = matrix
            mask[index, : matrix.shape[0]] = True
        similarities = np.einsum("qd,btd->bqt", q, padded, optimize=True)
        similarities = np.where(mask[:, None, :], similarities, -np.inf)
        scores.extend(float(value) for value in similarities.max(axis=2).sum(axis=1))
    return scores


async def _encode_in_batches(
    encoder: LateInteractionEncoder,
    texts: Sequence[str],
    *,
    batch_size: int,
    is_query: bool,
) -> list[np.ndarray]:
    encoded: list[np.ndarray] = []
    for offset in range(0, len(texts), batch_size):
        batch = texts[offset : offset + batch_size]
        values = await encoder.encode_queries(batch) if is_query else await encoder.encode_documents(batch)
        if len(values) != len(batch):
            raise RuntimeError("encoder_batch_size_mismatch")
        encoded.extend(np.asarray(value, dtype=np.float32) for value in values)
    return encoded


def create_app(
    config: ServiceConfig | None = None,
    *,
    encoder: LateInteractionEncoder | None = None,
    store: SQLiteMatrixStore | None = None,
) -> FastAPI:
    config = config or ServiceConfig.from_env()
    config.validate_startup()
    if encoder is None:
        encoder = _create_encoder(config)
    if encoder.model_revision != config.model_revision:
        raise RuntimeError("configured encoder model revision mismatch")
    if encoder.dimension != config.dimension:
        raise RuntimeError("configured encoder dimension mismatch")
    store = store or SQLiteMatrixStore(config.database_path)

    app = FastAPI(title="Project Brain Late Interaction Service", version="1")
    app.state.config = config
    app.state.encoder = encoder
    app.state.store = store
    app.state.semaphore = asyncio.Semaphore(config.max_concurrency)
    app.state.revision_lock = AsyncRevisionLock()
    app.state.identity_refresh_tasks = {}

    def require_approved_repository(repository_id: int) -> None:
        if config.approved_repository_id is not None and repository_id != config.approved_repository_id:
            raise HTTPException(
                status_code=403,
                detail="repository is not approved by service policy",
            )

    def inventory_policy_reason(inventory: IndexInventory) -> str:
        revision_reason = _revision_mismatch(
            requested_model=inventory.model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=inventory.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        if revision_reason:
            return revision_reason
        if inventory.document_count < config.minimum_document_count:
            return "document_count_below_service_minimum"
        return ""

    async def refresh_release_identity(
        repository_id: int,
    ) -> IndexReleaseIdentity:
        return await asyncio.to_thread(
            store.release_identity,
            repository_id,
            config.model_revision,
        )

    def schedule_release_identity_refresh(repository_id: int) -> None:
        """Coalesce maintenance refreshes without blocking a search request."""
        key = (repository_id, config.model_revision)
        existing = app.state.identity_refresh_tasks.get(key)
        if existing is not None and not existing.done():
            return

        async def refresh() -> None:
            try:
                await refresh_release_identity(repository_id)
            except Exception:
                logger.exception(
                    "late-interaction release identity refresh failed",
                    extra={"repository_id": repository_id},
                )
            finally:
                current = asyncio.current_task()
                if app.state.identity_refresh_tasks.get(key) is current:
                    app.state.identity_refresh_tasks.pop(key, None)

        app.state.identity_refresh_tasks[key] = asyncio.create_task(refresh())

    async def drain_release_identity_refreshes() -> None:
        tasks = list(app.state.identity_refresh_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app.router.add_event_handler("shutdown", drain_release_identity_refreshes)

    async def revision_read_guard():
        async with app.state.revision_lock.read():
            yield

    async def revision_write_guard():
        async with app.state.revision_lock.write():
            yield

    async def encode_available(
        texts: Sequence[str],
        *,
        batch_size: int,
        is_query: bool,
    ) -> list[np.ndarray]:
        try:
            return await _encode_in_batches(
                encoder,
                texts,
                batch_size=batch_size,
                is_query=is_query,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="late-interaction encoder unavailable",
            ) from exc

    async def require_token(
        supplied: Annotated[str | None, Header(alias="X-Late-Interaction-Token")] = None,
    ) -> None:
        candidate = supplied or ""
        if not hmac.compare_digest(candidate.encode("utf-8"), config.token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="invalid late-interaction token")

    auth = Depends(require_token)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "encoder_backend": config.encoder_backend,
            "model_revision": config.model_revision,
            "index_revision": store.global_revision(),
        }

    @app.get("/ready", dependencies=[auth])
    async def ready() -> dict[str, Any]:
        probe = getattr(encoder, "ready", None)
        if probe is not None:
            try:
                await probe()
            except Exception as exc:
                raise HTTPException(status_code=503, detail="late-interaction encoder unavailable") from exc
        payload: dict[str, Any] = {
            "status": "ready",
            "encoder_backend": config.encoder_backend,
            "model_revision": config.model_revision,
            "index_revision": store.global_revision(),
        }
        if config.approved_repository_id is not None:
            inventory = await asyncio.to_thread(
                store.cached_inventory,
                config.approved_repository_id,
                config.model_revision,
            )
            if inventory is None:
                schedule_release_identity_refresh(
                    config.approved_repository_id,
                )
                raise HTTPException(
                    status_code=503,
                    detail="identity_refresh_pending",
                )
            reason = inventory_policy_reason(inventory)
            if reason:
                raise HTTPException(status_code=503, detail=reason)
            payload.update(
                {
                    **inventory.__dict__,
                    "minimum_document_count": config.minimum_document_count,
                }
            )
        return payload

    @app.get("/v1/index/status", dependencies=[auth])
    async def index_status(repository_id: int, model_revision: str) -> dict[str, Any]:
        require_approved_repository(repository_id)
        inventory = await asyncio.to_thread(
            store.cached_inventory,
            repository_id,
            config.model_revision,
        )
        if inventory is None:
            schedule_release_identity_refresh(repository_id)
            summary = await asyncio.to_thread(
                store.inventory_summary,
                repository_id,
                config.model_revision,
            )
            return {
                "status": "not_ready",
                **summary.__dict__,
                "minimum_document_count": config.minimum_document_count,
                "reason": "identity_refresh_pending",
            }
        mismatch = _revision_mismatch(
            requested_model=model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=inventory.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        if mismatch:
            return {
                "status": "revision_mismatch",
                **inventory.__dict__,
                "minimum_document_count": config.minimum_document_count,
                "reason": mismatch,
            }
        if inventory.document_count < config.minimum_document_count:
            return {
                "status": "not_ready",
                **inventory.__dict__,
                "minimum_document_count": config.minimum_document_count,
                "reason": "document_count_below_service_minimum",
            }
        return {
            "status": "ready",
            **inventory.__dict__,
            "minimum_document_count": config.minimum_document_count,
            "reason": "",
        }

    @app.get("/v1/index/documents", dependencies=[auth])
    async def index_documents(
        repository_id: int,
        model_revision: str,
        after_chunk_id: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        require_approved_repository(repository_id)
        if after_chunk_id < 0 or not 1 <= limit <= 1000:
            raise HTTPException(
                status_code=422,
                detail="after_chunk_id must be non-negative and limit must be 1..1000",
            )
        current = store.index_revision(repository_id, config.model_revision)
        mismatch = _revision_mismatch(
            requested_model=model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=current,
            # Authenticated inventory access is the recovery plane. It must
            # remain readable while a new or restored index is below the
            # serving floor, otherwise remote sync cannot rebuild from r0.
            enforced_minimum_index=None,
        )
        if mismatch:
            return {
                "status": "revision_mismatch",
                "repository_id": repository_id,
                "model_revision": config.model_revision,
                "index_revision": current,
                "documents": [],
                "next_after_chunk_id": None,
                "reason": mismatch,
            }
        documents = await asyncio.to_thread(
            store.list_documents,
            repository_id,
            model_revision,
            after_chunk_id=after_chunk_id,
            limit=limit,
        )
        next_after = documents[-1]["chunk_id"] if len(documents) == limit else None
        return {
            "status": "ready",
            "repository_id": repository_id,
            "model_revision": config.model_revision,
            "index_revision": current,
            "documents": documents,
            "next_after_chunk_id": next_after,
            "reason": "",
        }

    @app.post(
        "/v1/index/finalize",
        dependencies=[auth, Depends(revision_read_guard)],
    )
    async def finalize_index(request: FinalizeRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        identity = await asyncio.to_thread(
            store.finalize_release_identity,
            request.repository_id,
            config.model_revision,
        )
        inventory = await asyncio.to_thread(
            store.cached_inventory,
            request.repository_id,
            config.model_revision,
        )
        if inventory is None:
            raise HTTPException(
                status_code=503,
                detail="identity_refresh_failed",
            )
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=identity.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        reason = mismatch or inventory_policy_reason(inventory)
        return {
            "status": "ready" if not reason else "not_ready",
            **inventory.__dict__,
            "minimum_document_count": config.minimum_document_count,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "reason": reason,
        }

    @app.post(
        "/v1/index/upsert",
        dependencies=[auth],
    )
    async def upsert(request: UpsertRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        if len(request.documents) > config.max_documents:
            raise HTTPException(status_code=413, detail="document batch exceeds service limit")
        if sum(len(document.text) for document in request.documents) > (config.max_documents * config.max_text_chars):
            raise HTTPException(status_code=413, detail="document payload exceeds service limit")
        if any(len(document.text) > config.max_text_chars for document in request.documents):
            raise HTTPException(status_code=413, detail="document text exceeds service limit")
        if any(
            not _valid_hash(
                document.content_hash,
                allow_empty=config.allow_empty_content_hash,
            )
            for document in request.documents
        ):
            raise HTTPException(status_code=422, detail="valid SHA-256 content_hash required")

        encoded: dict[tuple[int, str, str], np.ndarray] = {}
        chunk_ids = [document.chunk_id for document in request.documents]

        def matrix_key(document: DocumentInput) -> tuple[int, str, str]:
            return (
                document.chunk_id,
                document.path,
                document.content_hash.lower(),
            )

        def conflicting_documents(
            existing: dict[int, tuple[str, str]],
        ) -> list[DocumentInput]:
            return [
                document
                for document in request.documents
                if document.chunk_id in existing and existing[document.chunk_id][1] != document.content_hash.lower()
            ]

        def changed_documents(
            existing: dict[int, tuple[str, str]],
        ) -> list[DocumentInput]:
            return [
                document
                for document in request.documents
                if existing.get(document.chunk_id) != (document.path, document.content_hash.lower())
            ]

        while True:
            async with app.state.revision_lock.read():
                current = await asyncio.to_thread(
                    store.index_revision,
                    request.repository_id,
                    config.model_revision,
                )
                mismatch = _revision_mismatch(
                    requested_model=request.model_revision,
                    configured_model=config.model_revision,
                    requested_index=request.index_revision,
                    minimum_index=None,
                    current_index=current,
                )
                if mismatch:
                    return _mutation_response(
                        status="revision_mismatch",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason=mismatch,
                    )
                existing = await asyncio.to_thread(
                    store.document_identities,
                    request.repository_id,
                    request.model_revision,
                    chunk_ids,
                )
                if conflicting_documents(existing):
                    return _mutation_response(
                        status="identity_conflict",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason="immutable_chunk_content_hash_conflict",
                    )
                changed = changed_documents(existing)

            missing = [document for document in changed if matrix_key(document) not in encoded]
            if missing:
                async with app.state.semaphore:
                    matrices = await encode_available(
                        [document.text for document in missing],
                        batch_size=config.encoder_batch_size,
                        is_query=False,
                    )
                for document, matrix in zip(missing, matrices):
                    if matrix.ndim != 2 or matrix.shape[1] != config.dimension:
                        raise HTTPException(
                            status_code=502,
                            detail="encoder returned invalid matrix shape",
                        )
                    encoded[matrix_key(document)] = matrix
                continue

            async with app.state.revision_lock.write():
                current = await asyncio.to_thread(
                    store.index_revision,
                    request.repository_id,
                    config.model_revision,
                )
                mismatch = _revision_mismatch(
                    requested_model=request.model_revision,
                    configured_model=config.model_revision,
                    requested_index=request.index_revision,
                    minimum_index=None,
                    current_index=current,
                )
                if mismatch:
                    return _mutation_response(
                        status="revision_mismatch",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason=mismatch,
                    )
                existing = await asyncio.to_thread(
                    store.document_identities,
                    request.repository_id,
                    request.model_revision,
                    chunk_ids,
                )
                if conflicting_documents(existing):
                    return _mutation_response(
                        status="identity_conflict",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason="immutable_chunk_content_hash_conflict",
                    )
                changed = changed_documents(existing)
                if any(matrix_key(document) not in encoded for document in changed):
                    continue
                entries = [
                    StoredMatrix(
                        chunk_id=document.chunk_id,
                        path=document.path,
                        content_hash=document.content_hash.lower(),
                        matrix=encoded[matrix_key(document)],
                    )
                    for document in changed
                ]
                revision = await asyncio.to_thread(
                    store.put,
                    request.repository_id,
                    request.model_revision,
                    entries,
                )
                return _mutation_response(
                    status="updated" if changed else "unchanged",
                    model_revision=config.model_revision,
                    index_revision=revision,
                    started=started,
                    accepted=len(changed),
                    unchanged=len(request.documents) - len(changed),
                )

    @app.post(
        "/v1/index/delete",
        dependencies=[auth, Depends(revision_write_guard)],
    )
    async def delete(request: DeleteRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        current = store.index_revision(request.repository_id, config.model_revision)
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=request.index_revision,
            minimum_index=None,
            current_index=current,
        )
        if mismatch:
            return _mutation_response(
                status="revision_mismatch",
                model_revision=config.model_revision,
                index_revision=current,
                started=started,
                reason=mismatch,
            )
        deleted, revision = await asyncio.to_thread(
            store.delete,
            request.repository_id,
            request.model_revision,
            request.chunk_ids,
        )
        return _mutation_response(
            status="updated" if deleted else "unchanged",
            model_revision=config.model_revision,
            index_revision=revision,
            started=started,
            deleted=deleted,
        )

    @app.post(
        "/v1/index/prune",
        dependencies=[auth, Depends(revision_write_guard)],
    )
    async def prune(request: PruneRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        current = store.index_revision(request.repository_id, config.model_revision)
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=request.index_revision,
            minimum_index=None,
            current_index=current,
        )
        if mismatch:
            return _mutation_response(
                status="revision_mismatch",
                model_revision=config.model_revision,
                index_revision=current,
                started=started,
                reason=mismatch,
            )
        deleted, revision = await asyncio.to_thread(
            store.prune,
            request.repository_id,
            request.model_revision,
            request.keep_chunk_ids,
        )
        return _mutation_response(
            status="updated" if deleted else "unchanged",
            model_revision=config.model_revision,
            index_revision=revision,
            started=started,
            deleted=deleted,
        )

    @app.post(
        "/v1/rerank",
        dependencies=[auth, Depends(revision_read_guard)],
    )
    async def rerank(request: RerankRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        if len(request.query) > config.max_text_chars:
            raise HTTPException(
                status_code=413,
                detail="query text exceeds service limit",
            )
        identity = await asyncio.to_thread(
            store.cached_release_identity,
            request.repository_id,
            config.model_revision,
        )
        if identity is None:
            schedule_release_identity_refresh(request.repository_id)
            inventory = await asyncio.to_thread(
                store.inventory_summary,
                request.repository_id,
                config.model_revision,
            )
            pending_identity = IndexReleaseIdentity(
                repository_id=inventory.repository_id,
                model_revision=inventory.model_revision,
                index_revision=inventory.index_revision,
                lineage_id=inventory.lineage_id,
                identity_digest="",
                document_count=inventory.document_count,
            )
            return _rerank_response(
                status="index_not_ready",
                identity=pending_identity,
                started=started,
                reason="identity_refresh_pending",
            )
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=request.index_revision,
            minimum_index=request.minimum_index_revision,
            current_index=identity.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        if mismatch:
            return _rerank_response(
                status="revision_mismatch",
                identity=identity,
                started=started,
                reason=mismatch,
            )
        if identity.document_count < config.minimum_document_count:
            return _rerank_response(
                status="index_not_ready",
                identity=identity,
                started=started,
                reason="document_count_below_service_minimum",
            )
        requested_count = len(request.candidates)
        candidates = request.candidates[: config.max_candidates]
        stored = await asyncio.to_thread(
            store.fetch,
            request.repository_id,
            request.model_revision,
            [candidate.chunk_id for candidate in candidates],
        )
        eligible: list[tuple[CandidateInput, StoredMatrix]] = []
        for candidate in candidates:
            value = stored.get(candidate.chunk_id)
            if value is None:
                continue
            if not _valid_hash(candidate.content_hash, allow_empty=config.allow_empty_content_hash):
                continue
            if candidate.content_hash and not hmac.compare_digest(
                candidate.content_hash.lower(),
                value.content_hash,
            ):
                continue
            if not hmac.compare_digest(candidate.path, value.path):
                continue
            eligible.append((candidate, value))
        denominator = max(requested_count, 1)
        coverage = len(eligible) / denominator
        if not eligible:
            return _rerank_response(
                status="insufficient_coverage",
                identity=identity,
                started=started,
                coverage=coverage,
                reason="no_current_candidate_matrices",
            )
        async with app.state.semaphore:
            query_matrices = await encode_available(
                [request.query],
                batch_size=1,
                is_query=True,
            )
            scores = await asyncio.to_thread(
                _batched_maxsim,
                query_matrices[0],
                [value.matrix for _, value in eligible],
                batch_size=config.score_batch_size,
            )
        ranked: list[dict[str, Any]] = [
            {
                "chunk_id": candidate.chunk_id,
                "path": value.path,
                "score": score,
            }
            for (candidate, value), score in zip(eligible, scores)
        ]
        ranked.sort(
            key=lambda item: (
                -float(item["score"]),
                int(item["chunk_id"]),
            )
        )
        partial = requested_count > config.max_candidates or len(eligible) < len(candidates)
        return _rerank_response(
            status="partial" if partial else "scored",
            identity=identity,
            started=started,
            scores=ranked,
            coverage=coverage,
            reason="bounded_or_stale_candidates" if partial else "",
        )

    return app


def _mutation_response(
    *,
    status: str,
    model_revision: str,
    index_revision: str,
    started: float,
    accepted: int = 0,
    unchanged: int = 0,
    deleted: int = 0,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "accepted": accepted,
        "unchanged": unchanged,
        "deleted": deleted,
        "model_revision": model_revision,
        "index_revision": index_revision,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "reason": reason,
    }


def _rerank_response(
    *,
    status: str,
    identity: IndexReleaseIdentity,
    started: float,
    scores: list[dict[str, Any]] | None = None,
    coverage: float = 0.0,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "scores": scores or [],
        "coverage": coverage,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "repository_id": identity.repository_id,
        "model_revision": identity.model_revision,
        "index_revision": identity.index_revision,
        "lineage_id": identity.lineage_id,
        "identity_digest": identity.identity_digest,
        "document_count": identity.document_count,
        "reason": reason,
    }


def create_app_from_env() -> FastAPI:
    """Uvicorn factory; startup validation happens before the server binds."""
    return create_app(ServiceConfig.from_env())
