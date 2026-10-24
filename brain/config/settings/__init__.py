"""Process configuration: a single pydantic ``Settings`` assembled from
per-domain field mixins (``_core``, ``_infra``, ``_worker``, ``_team``,
``_late``, ``_retrieval``, ``_llm``, ``_agent``, ``_memory``) plus the
cross-domain ``model_validator``s in ``_model.py``.

``from brain.config.settings import settings`` is the process-wide instance."""
from __future__ import annotations

from ._env import _env_files as _env_files
from ._model import Settings as Settings

settings = Settings()
