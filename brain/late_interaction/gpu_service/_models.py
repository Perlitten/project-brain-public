"""Request/response pydantic models for the HTTP surface."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentInput(BaseModel):
    chunk_id: int
    path: str = Field(min_length=1, max_length=4096)
    content_hash: str
    text: str = Field(min_length=1)


class CandidateInput(BaseModel):
    chunk_id: int
    path: str = Field(min_length=1, max_length=4096)
    content_hash: str = ""


class UpsertRequest(BaseModel):
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    documents: list[DocumentInput] = Field(max_length=64)


class DeleteRequest(BaseModel):
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    chunk_ids: list[int] = Field(max_length=500)


class PruneRequest(BaseModel):
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    keep_chunk_ids: list[int] = Field(max_length=5000)


class FinalizeRequest(BaseModel):
    repository_id: int
    model_revision: str


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    # Requests above the 500 scoring cap are accepted and truncated so callers
    # still get a measured subset instead of a whole-request skip.
    candidates: list[CandidateInput] = Field(max_length=5000)
