"""Background job handlers — delegate to existing brain modules."""
from __future__ import annotations

from ._source_plan import (_MIRROR_FALLBACK_REASONS as _MIRROR_FALLBACK_REASONS, _SourcePlan as _SourcePlan, _mirror_plan as _mirror_plan, _prepare_source_revision as _prepare_source_revision, _recorded_mirror_plan as _recorded_mirror_plan, _index_with_plan as _index_with_plan, _record_source_mode as _record_source_mode, _repair_source_fingerprint as _repair_source_fingerprint)
from ._deep import (_NIGHTLY_QUALITY_PROBES as _NIGHTLY_QUALITY_PROBES, _deep_repo_path as _deep_repo_path, _approved_deep_repository as _approved_deep_repository, _reconcile_exact_lfm as _reconcile_exact_lfm, _run_deep_quality_probes as _run_deep_quality_probes, _standard_deep_context as _standard_deep_context)
from .jobs import (run_enrich_summaries as run_enrich_summaries, run_reindex as run_reindex, run_diff_review as run_diff_review, run_health_check as run_health_check, run_embedding_verify as run_embedding_verify, run_embedding_backfill as run_embedding_backfill, _run_embedding_backfill as _run_embedding_backfill, run_benchmark as run_benchmark, run_memory_consolidation as run_memory_consolidation, run_proactive_insights as run_proactive_insights, run_self_diagnosis as run_self_diagnosis, auto_heal_stale_repositories as auto_heal_stale_repositories, run_nightly_maintenance as run_nightly_maintenance, run_team_run_execution as run_team_run_execution, run_deep_context as run_deep_context)
from .dispatch import (validate_task_fencing as validate_task_fencing, execute_job as execute_job)

from brain.config.settings import settings as settings
from brain.workers.errors import (PermanentJobError as PermanentJobError,
                                  StaleWorkerFencingError as StaleWorkerFencingError)
