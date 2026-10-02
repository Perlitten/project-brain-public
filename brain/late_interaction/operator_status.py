"""Sanitized operator status for the optional late-interaction precision layer."""

from __future__ import annotations

import json
from typing import Any

from brain.config.settings import settings
from brain.database.session import redis_client
from brain.late_interaction.client import (
    DisabledLateInteractionClient,
    RemoteLateInteractionClient,
    build_maintenance_late_interaction_client,
    get_late_interaction_client,
)
from brain.late_interaction.metrics import snapshot as late_interaction_metrics
from brain.late_interaction.provider import get_lfm_colbert_provider
from brain.late_interaction.store import collect_late_interaction_inventory


def _traffic_state() -> dict[str, Any]:
    enabled = bool(settings.LATE_INTERACTION_ENABLED)
    dual_write = bool(settings.LATE_INTERACTION_DUAL_WRITE_ENABLED)
    shadow = bool(settings.LATE_INTERACTION_SHADOW_ENABLED)
    rerank = bool(settings.LATE_INTERACTION_RERANK_ENABLED)
    canary_percent = int(settings.LATE_INTERACTION_CANARY_PERCENT)
    remote_enabled = bool(settings.LATE_INTERACTION_REMOTE_ENABLED)

    if not enabled:
        mode = "off"
    elif rerank or canary_percent > 0:
        mode = "online"
    elif shadow:
        mode = "shadow"
    elif dual_write:
        mode = "indexing_only"
    else:
        mode = "armed_no_route"

    return {
        "remote_enabled": remote_enabled,
        "enabled": enabled,
        "dual_write": dual_write,
        "shadow": shadow,
        "shadow_persistence": bool(
            settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED
        ),
        "rerank": rerank,
        "canary_percent": canary_percent,
        "traffic_active": enabled and (shadow or rerank or canary_percent > 0),
        "effective_mode": mode,
        "minimum_coverage": float(
            settings.LATE_INTERACTION_MIN_CANDIDATE_COVERAGE
        ),
    }


def _approval_state() -> dict[str, Any]:
    evidence_path = settings.LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH
    return {
        "experiment_authorized": bool(
            settings.LATE_INTERACTION_EXPERIMENT_AUTHORIZED
        ),
        "production_gates_passed": bool(
            settings.LATE_INTERACTION_PRODUCTION_GATES_PASSED
        ),
        "approved_repository_id": (
            settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID
        ),
        "approved_build_sha": settings.LATE_INTERACTION_APPROVED_BUILD_SHA,
        "approved_source_digest": (
            settings.LATE_INTERACTION_APPROVED_SOURCE_DIGEST
        ),
        "approved_model_revision": (
            settings.LATE_INTERACTION_APPROVED_MODEL_REVISION
        ),
        "approved_index_revision": (
            settings.LATE_INTERACTION_APPROVED_INDEX_REVISION
        ),
        "approved_lineage_id": settings.LATE_INTERACTION_APPROVED_LINEAGE_ID,
        "approved_identity_digest": (
            settings.LATE_INTERACTION_APPROVED_IDENTITY_DIGEST
        ),
        "approved_document_count": (
            settings.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT
        ),
        # A server path is not useful in the browser and may disclose deployment
        # layout. The evidence digest is the public operator identity.
        "production_evidence_present": bool(evidence_path),
        "production_evidence_sha256": (
            settings.LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256
        ),
    }


async def collect_late_interaction_operator_status(
    repository: Any,
) -> dict[str, Any]:
    """Collect live, repository-scoped LFM state without exposing credentials."""

    traffic = _traffic_state()
    slow_lane_active = bool(
        settings.LATE_INTERACTION_DEEP_ENABLED
        or settings.NIGHTLY_DEEP_MAINTENANCE_ENABLED
    )
    if settings.LATE_INTERACTION_REMOTE_ENABLED:
        owned_client = None
        if (
            slow_lane_active
            and not traffic["traffic_active"]
            and repository.id
            == settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID
        ):
            owned_client = build_maintenance_late_interaction_client(
                repository_id=repository.id,
            )
            client: RemoteLateInteractionClient | DisabledLateInteractionClient = owned_client
        else:
            client = get_late_interaction_client()
        try:
            inventory = (await client.status(repository.id)).__dict__
            provider_health = (await client.ready()).__dict__
        finally:
            if owned_client is not None:
                await owned_client.aclose()
        serving_backend = "remote"
    else:
        inventory = (
            await collect_late_interaction_inventory(repository.id)
        ).to_dict()
        serving_backend = "local"
        try:
            provider_health = await get_lfm_colbert_provider().health()
        except Exception as exc:  # fail-open operator telemetry
            provider_health = {
                "status": "unhealthy",
                "error": type(exc).__name__,
            }

    approval = _approval_state()
    last_maintenance: dict[str, Any] | None = None
    try:
        raw_maintenance = await redis_client.get(
            "brain:nightly-maintenance:last-result"
        )
    except Exception:
        raw_maintenance = None
    if raw_maintenance:
        try:
            stored = json.loads(raw_maintenance)
            dense = stored.get("dense_after") or {}
            late = stored.get("late_interaction") or {}
            probes = stored.get("quality_probes") or []
            last_maintenance = {
                "status": stored.get("status"),
                "repository": stored.get("repository"),
                "dense_passed": bool(dense.get("pass")),
                "dense_current": dense.get(
                    "chunks_with_current_embeddings"
                ),
                "dense_eligible": dense.get("total_eligible_chunks"),
                "coverage_pct": dense.get("pgvector_coverage_pct"),
                "lfm_index_revision": late.get("index_revision"),
                "lfm_documents": late.get("verified_documents"),
                "lfm_changed": late.get("accepted"),
                "lfm_deleted": late.get("deleted"),
                "quality_scored": sum(
                    1
                    for probe in probes
                    if probe.get("passed")
                ),
                "quality_total": len(probes),
                "telegram_status": (
                    stored.get("telegram") or {}
                ).get("status"),
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            last_maintenance = {"status": "unreadable"}
    # Keep the original flat `gates` object for API compatibility while the
    # dashboard consumes the clearer traffic/approval split.
    gates = {
        "enabled": traffic["enabled"],
        "dual_write": traffic["dual_write"],
        "shadow": traffic["shadow"],
        "shadow_persistence": traffic["shadow_persistence"],
        "rerank": traffic["rerank"],
        "canary_percent": traffic["canary_percent"],
        "minimum_coverage": traffic["minimum_coverage"],
        **approval,
    }
    return {
        "repository": {"id": repository.id, "path": repository.path},
        "serving_backend": serving_backend,
        "inventory": inventory,
        "provider": provider_health,
        "metrics": late_interaction_metrics(),
        "traffic": traffic,
        "approval": approval,
        "slow_lane": {
            "enabled": bool(settings.LATE_INTERACTION_DEEP_ENABLED),
            "nightly_enabled": bool(
                settings.NIGHTLY_DEEP_MAINTENANCE_ENABLED
            ),
            "repository_path": (
                settings.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH
            ),
            "timeout_s": settings.LATE_INTERACTION_DEEP_TIMEOUT_S,
            "vector_limit": settings.LATE_INTERACTION_DEEP_VECTOR_LIMIT,
            "notify": bool(settings.LATE_INTERACTION_DEEP_NOTIFY),
            "last_maintenance": last_maintenance,
        },
        "gates": gates,
    }
