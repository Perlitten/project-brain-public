"""Hybrid code search utilities."""

from brain.search.code_search import extract_keywords, search_code, vector_search_chunks
from brain.search.filters import (
    EXCLUDED_DIR_NAMES,
    get_file_type_weight,
    should_exclude_from_retrieval,
    should_ignore_dir,
)
from brain.search.similarity import cosine_similarity

__all__ = [
    "cosine_similarity",
    "extract_keywords",
    "get_file_type_weight",
    "search_code",
    "should_exclude_from_retrieval",
    "should_ignore_dir",
    "vector_search_chunks",
    "EXCLUDED_DIR_NAMES",
]
