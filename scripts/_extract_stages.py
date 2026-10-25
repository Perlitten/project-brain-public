"""Extract stage blocks of HybridRetrievalPipeline.run into _stage_* methods.

Line ranges are 1-based inclusive, in _pipeline.py coordinates. Bodies move
verbatim (already 8-indented). run() keeps signature, setup, tail."""
from pathlib import Path

P = Path("brain/retrieval/pipeline/_pipeline.py")
lines = P.read_text(encoding="utf-8").splitlines()

# (name, start, end, returns, params)
STAGES = [
    ("lexical_symbols", 85, 186, "lexical_files, lexical_symbols, symbol_paths, hint_files",
     "keywords_lower, repository_id, task_description, v6_enabled, candidates_by_channel, timing"),
    ("vector", 187, 228, "vector_result, chunk_files",
     "task_description, deep_mode, v6_enabled, repository_id, v2_enabled, candidates_by_channel, timing"),
    ("cards", 229, 241, "card_by_path",
     "task_description, repository_id, card_recall, card_rerank, candidates_by_channel, timing"),
    ("graph", 242, 284, "graph_counts",
     "repository_id, route, keywords_lower, candidates_by_channel, timing"),
    ("memory", 285, 296, None,
     "repository_id, keywords_lower, candidates_by_channel, timing"),
    ("bm25_fallback", 297, 311, None,
     "v2_enabled, repository_id, task_description, candidates_by_channel"),
    ("fusion", 312, 350, "prepared_fusion, fused_ranking, channel_paths, path_to_file",
     "candidates_by_channel, route, card_recall, lexical_files, hint_files, chunk_files, card_by_path,"
     " repository_id, v2_enabled, v6_enabled, expected_surface, task_type, task_description,"
     " keywords_lower, timing"),
    ("rerank_boosts", 351, 424, "reranked, probe_list, hint_tokens, v2_rerank_off",
     "fused_ranking, keywords_lower, task_description, intent, candidates_by_channel, graph_counts,"
     " route, path_to_file, task_type, v2_enabled, timing"),
    ("recall_pool", 425, 508, "recall, pool_candidates",
     "v6_enabled, reranked, expected_surface, file_limit, repository_id, path_to_file, intent,"
     " task_type, timing"),
    ("v5_rerank", 509, 554,
     "precision_k, commit_hash, v5_result, baseline_v5_top, available,"
     " vector_by_path, lexical_by_path, symbol_by_path, graph_by_path",
     "candidates_by_channel, pool_candidates, path_to_file, card_by_path, card_rerank, file_limit,"
     " repository_id, v2_rerank_off, reranked, task_description, task_type, repository_name,"
     " v6_enabled, expected_surface, timing"),
    ("baseline_selection", 555, 589, "pinned, baseline_selected",
     "probe_list, v2_rerank_off, repository_id, path_to_file, hint_tokens, file_limit,"
     " candidates_by_channel, reranked, baseline_v5_top, precision_k, keywords_lower, task_type,"
     " task_description, timing"),
    ("late_interaction", 590, 713, "selected, v5_top, v5_debug_result, reranked, late_debug",
     "repository_id, deep_mode, task_description, pool_candidates, vector_result, reranked,"
     " path_to_file, vector_by_path, lexical_by_path, symbol_by_path, graph_by_path, card_by_path,"
     " card_rerank, available, precision_k, expected_surface, v6_enabled, commit_hash, task_type,"
     " repository_name, file_limit, candidates_by_channel, keywords_lower, pinned, baseline_selected,"
     " baseline_v5_top, v5_result, late_interaction_client, timing"),
]

def trim(s, e):
    while e > s and not lines[e - 1].strip():
        e -= 1
    while s <= e and not lines[s - 1].strip():
        s += 1
    return s, e

methods = []
call_lines = []
for name, s, e, ret, params in STAGES:
    s, e = trim(s, e)
    body = lines[s - 1:e]
    sig_params = ", ".join(p.strip() for p in params.split(","))
    meth = [f"    async def _stage_{name}(self, *, {sig_params}):"]
    meth += body
    if ret:
        meth.append(f"        return {ret}")
    methods.append("\n".join(meth))
    call = ", ".join(f"{p.strip()}={p.strip()}" for p in params.split(","))
    if ret:
        call_lines.append(f"        ({ret}) = await self._stage_{name}(")
    else:
        call_lines.append(f"        await self._stage_{name}(")
    call_lines.append(f"            {call})")

# run(): keep L39-50 sig + L51-84 setup, then calls, then tail L714-780
new_run = lines[38:84] + call_lines + [""] + lines[713:780]

head = lines[:38]                     # imports through __init__ end
out = head + new_run + ["", ""] + methods + [""]
P.write_text("\n".join(out), encoding="utf-8")
print("written; run() now", len(new_run) - 39, "lines")
