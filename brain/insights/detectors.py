"""Pure, ordered detectors over an already collected operational snapshot.

No database, filesystem, transport, model or global configuration dependency.
The caller supplies policy; each detector owns one family of snapshot checks.
Partial snapshot handling and record ordering are part of the legacy contract.
"""

from __future__ import annotations

from typing import Any

from brain.insights.candidates import InsightCandidate, _clean_text, _safe_count, _safe_token


def operational_insights(snapshot: dict[str, Any], *, environment: str) -> list[InsightCandidate]:
    insights: list[InsightCandidate] = []
    services = snapshot.get("services") or {}
    project = snapshot.get("project") or {}
    alerting = snapshot.get("alerting") or {}
    trigger = snapshot.get("trigger") or {}

    if trigger:
        source = _safe_token(trigger.get("source"), fallback="external")
    else:
        source = ""
    is_failure_trigger = source in {
        "workflow_error",
        "worker_error",
        "worker_failure",
        "job_failure",
    } or source.endswith(("_error", "_failure", "_failed"))
    if trigger and is_failure_trigger:
        reference = _clean_text(trigger.get("reference"), limit=255)
        insights.append(
            InsightCandidate(
                insight_type="automation_trigger",
                severity="warning",
                title="An automation failure triggered self-diagnosis",
                summary=_clean_text(
                    trigger.get("summary") or "An external automation reported a failure.",
                    limit=1000,
                ),
                evidence=[
                    {"label": "source", "value": source},
                    {"label": "reference", "value": reference or "unavailable"},
                ],
                recommended_action="Correlate the trigger reference with the worker logs and GET /jobs/<id>, then re-run the job after remediation.",
                confidence="high",
                dedupe_key=f"automation_trigger:{source}:{reference or 'unknown'}",
            )
        )

    core_unhealthy = [
        name for name in ("postgres", "redis", "neo4j") if (services.get(name) or {}).get("status") != "healthy"
    ]
    if core_unhealthy:
        insights.append(
            InsightCandidate(
                insight_type="service_health",
                severity="critical",
                title="Core service requires attention",
                summary=f"Core services are not all healthy: {', '.join(core_unhealthy)}.",
                evidence=[
                    {"label": name, "value": (services.get(name) or {}).get("error") or "unhealthy"}
                    for name in core_unhealthy
                ],
                recommended_action="Open dashboard logs and restore the unhealthy datastore before relying on retrieval or automation.",
                confidence="high",
                dedupe_key="service_health:core_unhealthy",
            )
        )

    stale_jobs = list((snapshot.get("scheduler") or {}).get("stale") or [])
    if stale_jobs:
        insights.append(
            InsightCandidate(
                insight_type="automation_health",
                severity="warning",
                title="Scheduled jobs are overdue",
                summary=f"No successful run within the expected interval for: {', '.join(stale_jobs)}.",
                evidence=[{"label": "stale_jobs", "value": ", ".join(stale_jobs)}],
                recommended_action="Check that a worker is running (SCHEDULER_ENABLED=true) and its logs; use Run now on Settings → Scheduler or POST /jobs/* meanwhile.",
                confidence="high",
                dedupe_key="automation_health:scheduler_stale",
            )
        )

    if environment.lower() == "production" and (
        project.get("build_sha") in {None, "", "unknown"} or project.get("source_digest") in {None, "", "unknown"}
    ):
        insights.append(
            InsightCandidate(
                insight_type="release_identity",
                severity="critical",
                title="Production build identity is unverifiable",
                summary="The running API does not expose both a source revision and source digest.",
                evidence=[
                    {"label": "build_sha", "value": project.get("build_sha") or "unknown"},
                    {"label": "source_digest", "value": project.get("source_digest") or "unknown"},
                ],
                recommended_action="Redeploy through deploy/server_up.sh with a valid release marker and verify /api/version.",
                confidence="high",
                dedupe_key="release_identity:unverifiable",
            )
        )

    if alerting.get("self_diagnosis_enabled") and (
        not alerting.get("telegram_enabled") or not alerting.get("telegram_credentials_present")
    ):
        insights.append(
            InsightCandidate(
                insight_type="alert_delivery",
                severity="critical",
                title="Self-diagnosis has no usable Telegram channel",
                summary="Autonomous diagnosis is enabled but its owner alert channel is disabled or missing credentials.",
                evidence=[
                    {
                        "label": "telegram_enabled",
                        "value": alerting.get("telegram_enabled"),
                    },
                    {
                        "label": "telegram_credentials_present",
                        "value": alerting.get("telegram_credentials_present"),
                    },
                ],
                recommended_action="Configure the Telegram bot token and chat id, enable alerts, then run a delivery smoke test.",
                confidence="high",
                dedupe_key="alert_delivery:telegram_unavailable",
            )
        )

    return insights


def repository_insights(snapshot: dict[str, Any]) -> list[InsightCandidate]:
    insights: list[InsightCandidate] = []
    graph_identity = snapshot.get("graph_identity") or {}
    repositories = snapshot.get("repositories") or []

    legacy_unscoped = graph_identity.get("legacy_unscoped_nodes")
    if legacy_unscoped is not None and _safe_count(legacy_unscoped):
        insights.append(
            InsightCandidate(
                insight_type="graph_identity",
                severity="warning",
                title="Legacy unscoped graph nodes remain",
                summary="Strict repository reads ignore legacy Neo4j nodes that have no repository identity.",
                evidence=[
                    {
                        "label": "legacy_unscoped_nodes",
                        "value": legacy_unscoped,
                    }
                ],
                recommended_action="Reindex each active repository under graph schema v2, then remove only the verified legacy residue.",
                confidence="high",
                dedupe_key="graph_identity:legacy_unscoped_nodes",
            )
        )

    bad_repositories = [
        repository for repository in repositories if ((repository.get("freshness") or {}).get("status") != "current")
    ]
    if bad_repositories:
        critical_statuses = {"source_missing", "unknown"}
        severity = (
            "critical"
            if any(
                (repository.get("freshness") or {}).get("status") in critical_statuses
                for repository in bad_repositories
            )
            else "warning"
        )
        insights.append(
            InsightCandidate(
                insight_type="repository_freshness",
                severity=severity,
                title="Repository freshness is not fully verified",
                summary=f"{len(bad_repositories)} indexed repositories are not in a proven current state.",
                evidence=[
                    {
                        "label": str(repository.get("path") or repository.get("name")),
                        "value": (repository.get("freshness") or {}).get("status"),
                    }
                    for repository in bad_repositories[:10]
                ],
                recommended_action="Restore the source or manifest lineage, then reindex and verify embeddings.",
                confidence="high",
                dedupe_key="repository_freshness:not_current",
            )
        )

    return insights


def worker_insights(snapshot: dict[str, Any]) -> list[InsightCandidate]:
    insights: list[InsightCandidate] = []
    harness = snapshot.get("harness") or {}
    worker_queue = snapshot.get("worker_queue") or {}

    stale_task_count = _safe_count(harness.get("stale_task_count"))
    if stale_task_count:
        insights.append(
            InsightCandidate(
                insight_type="harness_lifecycle",
                severity="warning",
                title="Harness tasks are stuck beyond their timeout",
                summary=f"{stale_task_count} non-terminal tasks have exceeded their configured timeout.",
                evidence=[
                    {"label": "stale_task_count", "value": stale_task_count},
                    {
                        "label": "status_counts",
                        "value": harness.get("status_counts") or {},
                    },
                    {
                        "label": "expired_lease_count",
                        "value": harness.get("expired_lease_count"),
                    },
                ],
                recommended_action="Run task reconciliation, inspect expired leases, and route or close orphaned tasks.",
                confidence="high",
                dedupe_key="harness_lifecycle:stale_tasks",
            )
        )

    stale_worker_job_count = _safe_count(worker_queue.get("stale_job_count"))
    if stale_worker_job_count:
        insights.append(
            InsightCandidate(
                insight_type="worker_health",
                severity="critical",
                title="Durable worker jobs are stuck",
                summary=f"{stale_worker_job_count} queued, running, or retrying jobs exceeded their recovery window.",
                evidence=[
                    {
                        "label": "queue_state",
                        "value": {key: worker_queue.get(key) for key in ("queued", "processing", "retrying")},
                    },
                    {
                        "label": "stale_jobs",
                        "value": worker_queue.get("stale_jobs") or [],
                    },
                ],
                recommended_action="Inspect worker liveness and processing/retry lists, then restart the worker so its crash recovery can requeue the jobs.",
                confidence="high",
                dedupe_key="worker_health:stale_durable_jobs",
            )
        )

    return insights


def retrieval_insights(snapshot: dict[str, Any], *, minimum_recall: float) -> list[InsightCandidate]:
    insights: list[InsightCandidate] = []
    evaluation = snapshot.get("evaluation") or {}
    embeddings = snapshot.get("embeddings") or {}

    avg_recall = evaluation.get("avg_recall")
    if "evaluation" in snapshot and avg_recall is None:
        insights.append(
            InsightCandidate(
                insight_type="retrieval_quality",
                severity="warning",
                title="Retrieval quality has no current measurement",
                summary="No parseable golden evaluation report is available to prove retrieval quality.",
                evidence=[
                    {
                        "label": "avg_recall",
                        "value": "missing",
                    }
                ],
                recommended_action="Run the weekly benchmark and retain its eval-report.md in the configured report store.",
                confidence="high",
                dedupe_key="retrieval_quality:evaluation_missing",
            )
        )
    elif "evaluation" in snapshot:
        try:
            recall_value = float(avg_recall or 0)
        except (TypeError, ValueError):
            recall_value = 0.0
        if recall_value < minimum_recall:
            insights.append(
                InsightCandidate(
                    insight_type="retrieval_quality",
                    severity="critical",
                    title="Retrieval recall is below the production floor",
                    summary="The latest golden evaluation does not meet the configured minimum average recall.",
                    evidence=[
                        {"label": "avg_recall", "value": recall_value},
                        {
                            "label": "minimum_avg_recall",
                            "value": minimum_recall,
                        },
                    ],
                    recommended_action="Inspect missing expected files, repair indexing/routing, and rerun the benchmark before claiming readiness.",
                    confidence="high",
                    dedupe_key="retrieval_quality:recall_below_floor",
                )
            )

    missing = _safe_count(embeddings.get("missing_embeddings"))
    stale = _safe_count(embeddings.get("stale_embeddings"))
    incompatible = _safe_count(embeddings.get("incompatible_embeddings"))
    coverage = embeddings.get("pgvector_coverage_pct")
    # The stored figure is round(x, 2), so a corpus one chunk short of complete
    # records as 100.0 — and this insight then states 100% coverage directly
    # beside its own count of the chunks that have none. An incomplete corpus is
    # capped just below the clean number so the evidence cannot contradict
    # itself; a genuinely complete one is left alone.
    if coverage is not None and (missing or stale or incompatible):
        try:
            coverage = min(float(coverage), 99.9)
        except (TypeError, ValueError):
            pass
    if missing or stale or incompatible:
        severity = "critical" if incompatible else "warning"
        insights.append(
            InsightCandidate(
                insight_type="retrieval_health",
                severity=severity,
                title="Semantic retrieval coverage needs repair",
                summary="Some eligible chunks are missing current vectors, so retrieval can silently miss relevant code.",
                evidence=[
                    {"label": "missing_embeddings", "value": missing},
                    {"label": "stale_embeddings", "value": stale},
                    {"label": "incompatible_embeddings", "value": incompatible},
                    {"label": "pgvector_coverage_pct", "value": coverage},
                ],
                recommended_action="Run the embedding backfill job, then verify embeddings before trusting context packs.",
                confidence="high",
                dedupe_key="retrieval_health:embedding_inventory_not_clean",
            )
        )

    return insights


def indexing_insights(snapshot: dict[str, Any]) -> list[InsightCandidate]:
    insights: list[InsightCandidate] = []
    counts = snapshot.get("counts") or {}
    runs = snapshot.get("recent_indexing_runs") or []

    if _safe_count(counts.get("files")) == 0 or _safe_count(counts.get("chunks")) == 0:
        insights.append(
            InsightCandidate(
                insight_type="indexing_health",
                severity="critical",
                title="No indexed source inventory is available",
                summary="Project Brain has no useful file/chunk inventory for the target repository.",
                evidence=[
                    {"label": "files", "value": counts.get("files")},
                    {"label": "chunks", "value": counts.get("chunks")},
                    {"label": "repo_path", "value": (snapshot.get("repo") or {}).get("path")},
                ],
                recommended_action="Run a repository index job and verify embeddings after it completes.",
                confidence="high",
                dedupe_key="indexing_health:no_source_inventory",
            )
        )

    latest_run = runs[0] if runs else None
    if not latest_run:
        insights.append(
            InsightCandidate(
                insight_type="indexing_health",
                severity="info",
                title="Indexing history is empty",
                summary="No indexing run is recorded yet, so operators cannot tell when this repo was last scanned.",
                evidence=[{"label": "recent_indexing_runs", "value": 0}],
                recommended_action="Run an initial index job or confirm the target repository path is correct.",
                confidence="medium",
                dedupe_key="indexing_health:no_indexing_history",
            )
        )
    elif str(latest_run.get("status")).lower() == "failed":
        insights.append(
            InsightCandidate(
                insight_type="indexing_health",
                severity="warning",
                title="Latest indexing run failed",
                summary="The newest repository scan did not complete successfully.",
                evidence=[
                    {"label": "run_id", "value": latest_run.get("id")},
                    {"label": "status", "value": latest_run.get("status")},
                    {"label": "started_at", "value": latest_run.get("started_at")},
                ],
                recommended_action="Inspect worker logs, fix the indexing error, and rerun indexing without clean mode first.",
                confidence="high",
                dedupe_key=f"indexing_health:latest_failed:{latest_run.get('id')}",
            )
        )

    return insights


def recent_job_insights(snapshot: dict[str, Any]) -> list[InsightCandidate]:
    insights: list[InsightCandidate] = []
    jobs = snapshot.get("recent_jobs") or []

    failed_jobs = [job for job in jobs if str(job.get("status")).lower() == "failed"]
    if failed_jobs:
        job = failed_jobs[0]
        insights.append(
            InsightCandidate(
                insight_type="worker_health",
                severity="warning",
                title="Recent background job failed",
                summary=f"A recent {job.get('type') or 'worker'} job failed and may require operator follow-up.",
                evidence=[
                    {"label": "job_id", "value": job.get("id")},
                    {"label": "job_type", "value": job.get("type")},
                    {"label": "error", "value": job.get("error") or "failed"},
                ],
                recommended_action="Open the recent job status and worker logs before scheduling more dependent work.",
                confidence="medium",
                dedupe_key=f"worker_health:failed_job:{job.get('id')}",
            )
        )

    return insights


def memory_insights(snapshot: dict[str, Any]) -> list[InsightCandidate]:
    insights: list[InsightCandidate] = []
    counts = snapshot.get("counts") or {}

    if _safe_count(counts.get("rules")) == 0 or _safe_count(counts.get("decisions")) == 0:
        insights.append(
            InsightCandidate(
                insight_type="architecture_memory",
                severity="info",
                title="Architecture memory is sparse",
                summary="Rules or decisions are empty, so Brain has fewer constraints to enforce during code work.",
                evidence=[
                    {"label": "rules", "value": counts.get("rules")},
                    {"label": "decisions", "value": counts.get("decisions")},
                ],
                recommended_action="Import or create the key ADRs and operating rules for this codebase.",
                confidence="medium",
                dedupe_key="architecture_memory:sparse_rules_or_decisions",
            )
        )

    return insights
