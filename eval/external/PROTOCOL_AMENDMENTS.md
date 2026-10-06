# Protocol amendments

All amendments are made **before the corresponding runs** and state their reason.
Amendments after a run that touch its instances would invalidate that run's numbers.

## A1 — 2026-10-06 21:45 UTC (before any run)

**Query formulation**: `ContextPackBuilder._classify_task` is deterministic by
default (`RETRIEVAL_USE_LLM_CLASSIFICATION=false` in shipped .env): it derives
`task_type` by keyword heuristics and `keywords` = dedup'd `extract_keywords(issue)`
with no cap. Therefore:

- `brain-v5`/`brain-v6` use the **prod** query understanding: `task_type` and
  `keywords` come from `ContextPackBuilder()._classify_task(issue_text)` — not a
  frozen surrogate. This is identical to what `/context` packs compute.
- `brain-mockllm` is redundant (the prod formulation is already LLM-free) → arm
  dropped; the gap is already disclosed under "Known gaps".

**nolex mechanics** (§1): implemented by patching `pipeline.expand_keywords`
and `pipeline.derive_path_hints` to return `[]` for the arm's run — removes
lexical/symbol/hints channels and keyword-path rerank bonuses in one stroke.

**grep baseline** (§1): one invocation `rg -i -c --fixed-strings -e kw1 ... -e kwN`
(keywords = prod `_classify_task` keywords, capped at 40 for runtime); file score =
summed match count across keywords; ties by path. Keywords >200 chars excluded.

**bm25 tokenizer** (§1): `re.findall(r"\w+", text.lower())` on chunk content and
on issue text; file score = max chunk BM25 score; top-30 retained.

**file_limit for v5**: `file_limit=30` keeps the v5 path (`RETRIEVAL_V6_ENABLED`
is False in shipped defaults; the 30-item `selected_paths` is the "deep" pack
budget). `brain-v6` repeats the run with the flag toggled to True in-process.
