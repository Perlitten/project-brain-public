"""Aggregate router for the core API surface.

The endpoints live in domain modules — ``core_lifecycle``, ``core_retrieval``,
``core_memory``, ``core_features``, ``core_status`` — included here in the
original registration order, so paths, operation ids and the generated
OpenAPI document are unchanged.

Re-exports below keep existing importers working: ``apps.mcp_server`` imports
``_ask_v2``, ``apps.api.routers.telegram_bridge`` imports ``ask_project``, and
tests patch attributes on the shared ``settings`` / ``redis_client`` singletons
and the store classes through this module.
"""

from fastapi import APIRouter

from apps.api.routers import (
    core_features,
    core_lifecycle,
    core_memory,
    core_retrieval,
    core_status,
)
from apps.api.routers.core_retrieval import (  # noqa: F401  (re-export)
    ASK_RETRIEVAL_LIMIT,
    _ask_v2,
    _strip_thinking_blocks,
    ask_project,
    build_task_context,
    search_code_endpoint,
)
from brain.config.settings import settings  # noqa: F401  (re-export)
from brain.database.session import redis_client  # noqa: F401  (re-export)
from brain.memory.decision_store import DecisionStore  # noqa: F401  (re-export)
from brain.memory.rule_store import RuleStore  # noqa: F401  (re-export)

router = APIRouter()
router.include_router(core_lifecycle.router)
router.include_router(core_retrieval.router)
router.include_router(core_memory.router)
router.include_router(core_features.router)
router.include_router(core_status.router)
