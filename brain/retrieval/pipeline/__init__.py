"""Hybrid retrieval pipeline: query understanding → channels → fusion → rerank."""
from __future__ import annotations

from ._channels import (classify_query_route as classify_query_route, _channel_weight as _channel_weight, _path_keyword_hits as _path_keyword_hits, _channel_strength as _channel_strength, select_surface_balanced_pool as select_surface_balanced_pool, _synth_pool_candidate as _synth_pool_candidate, _should_abstain as _should_abstain, _lexical_file_candidate as _lexical_file_candidate)
from ._terms import (_IDENT_RE as _IDENT_RE, _PATH_TOKEN_RE as _PATH_TOKEN_RE, _SOURCE_FILE_EXTENSIONS as _SOURCE_FILE_EXTENSIONS, _extract_file_paths as _extract_file_paths, _ERROR_NAME_RE as _ERROR_NAME_RE, _STACK_FRAME_RE as _STACK_FRAME_RE, _ID_QUERY_STOPWORDS as _ID_QUERY_STOPWORDS, extract_code_query_terms as extract_code_query_terms, _TOKEN_RE as _TOKEN_RE, _bm25_tokenize as _bm25_tokenize, _content_bm25_channel as _content_bm25_channel)
from ._late import (_late_canary_selected as _late_canary_selected, _LateResult as _LateResult, _await_late_rerank as _await_late_rerank, _apply_late_interaction_scores as _apply_late_interaction_scores, _content_hash as _content_hash, _resolve_late_interaction_candidates as _resolve_late_interaction_candidates, _build_rerank_pool as _build_rerank_pool, _rerank_late_counterfactual as _rerank_late_counterfactual)
from ._fusion import (_LIN_CHANNEL_WEIGHTS_ATTRS as _LIN_CHANNEL_WEIGHTS_ATTRS, _fusion_policy as _fusion_policy, _v2_fused_scores as _v2_fused_scores)
from ._select import (_pin_qualified_path_hints as _pin_qualified_path_hints, _select_final_paths as _select_final_paths)
from ._pipeline import (HybridRetrievalPipeline as HybridRetrievalPipeline)

from brain.retrieval.fusion import _content_match_bonus as _content_match_bonus
from brain.retrieval.types import QueryRoute as QueryRoute
