"""Authenticated late-interaction service with a local SQLite matrix store."""
from __future__ import annotations

from ._config import (_SHA256_RE as _SHA256_RE, logger as logger, ServiceConfig as ServiceConfig, _env_bool as _env_bool, _env_optional_int as _env_optional_int, _create_encoder as _create_encoder, LateInteractionEncoder as LateInteractionEncoder)
from ._store import (AsyncRevisionLock as AsyncRevisionLock, StoredMatrix as StoredMatrix, IndexInventory as IndexInventory, IndexReleaseIdentity as IndexReleaseIdentity, SQLiteMatrixStore as SQLiteMatrixStore)
from ._models import (DocumentInput as DocumentInput, CandidateInput as CandidateInput, UpsertRequest as UpsertRequest, DeleteRequest as DeleteRequest, PruneRequest as PruneRequest, FinalizeRequest as FinalizeRequest, RerankRequest as RerankRequest)
from ._app import (_valid_hash as _valid_hash, _revision_mismatch as _revision_mismatch, _revision_number as _revision_number, _batched_maxsim as _batched_maxsim, _encode_in_batches as _encode_in_batches, create_app as create_app, _mutation_response as _mutation_response, _rerank_response as _rerank_response, create_app_from_env as create_app_from_env)
