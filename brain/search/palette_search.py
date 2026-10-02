"""Lexical-only code lookup for the dashboard command palette.

Why this exists as its own module rather than a flag on `search_code`.

`search_code` is the retrieval authority: /ask, context packs and the MCP tools
all read its ranking, and its chunk channel is the vector one — it embeds the
query over the network before pgvector can be asked. On production that round
trip is most of a ~3.7s call. The palette is driven by a debounced keystroke and
never renders chunks: it lists file and symbol *records*. It was paying the full
embedding latency for a channel it discards, then being cut off by a 1.2s budget
and reporting `code unavailable` on every single query.

So the palette gets a path that never embeds:

  files    one bounded SELECT over File.path / File.summary (ILIKE per keyword),
           re-ordered by `score_path_for_keywords` — the same lexical scorer the
           context-pack builder already uses.
  symbols  one bounded SELECT over Symbol.name / Symbol.summary, exact and
           prefix matches on the name first.

Nothing here changes how `search_code` ranks anything. It is a second, narrower
reader of the same two tables, and `search_code` keeps its vector path exactly as
it was — the retrieval evaluation that ranking is measured by does not run
through this module.

The SQL bounds mirror the palette's own: at most `limit * 10` file rows and
`limit * 4` symbol rows are read per request, and the retrieval exclusions
(`should_exclude_from_retrieval`) are applied so the palette never offers a file
the rest of the system refuses to retrieve.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import false, or_, select

from brain.database.models import File, Symbol
from brain.database.session import async_session_factory
from brain.search.code_search import extract_keywords
from brain.search.filters import should_exclude_from_retrieval
from brain.search.lexical_ranking import score_path_for_keywords

# Rows read per request, as a multiple of the caller's result limit. Both are
# the ceiling on the SELECT, not on the answer: the scorer needs candidates to
# choose between, and the caller still slices to `limit`.
FILE_CANDIDATE_FACTOR = 10
SYMBOL_CANDIDATE_FACTOR = 4


def _symbol_rank(name: Optional[str], query: str, keywords: List[str]) -> float:
    """Order symbols by how directly the name answers the query.

    Deliberately simpler than the path scorer: a symbol has no path segments to
    weigh, so the only signals are the whole-query match and how much of the
    name a keyword accounts for.
    """
    lowered = (name or "").lower()
    if not lowered:
        return 0.0
    normalized = query.strip().lower()
    score = 0.0
    if lowered == normalized:
        score += 10.0
    elif normalized and lowered.startswith(normalized):
        score += 6.0
    elif normalized and normalized in lowered:
        score += 3.0
    for keyword in keywords:
        if not keyword:
            continue
        if lowered == keyword:
            score += 5.0
        elif lowered.startswith(keyword):
            score += 2.5
        elif keyword in lowered:
            score += 1.0
    # Shorter names win ties: `search_code` beats `search_code_lexical_helper`
    # when both merely contain the keyword.
    return score - min(len(lowered), 80) / 400.0


async def search_code_lexical(
    query: str,
    limit: int = 5,
    repository_id: int | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Rank files and symbols for `query` without embedding it.

    Returns the same two keys, with the same item shape, that the palette reads
    off `search_code` — `files` carry path/language/summary/file_type, `symbols`
    carry name/kind/signature/summary — so the caller is unchanged apart from
    which function it calls.
    """
    text = (query or "").strip()
    if not text:
        return {"files": [], "symbols": []}

    keywords = extract_keywords(text)
    if not keywords:
        return {"files": [], "symbols": []}

    async with async_session_factory() as session:
        file_clauses = [File.path.ilike(f"%{kw}%") for kw in keywords] + [
            File.summary.ilike(f"%{kw}%") for kw in keywords
        ]
        file_stmt = select(File).where(
            or_(*file_clauses) if file_clauses else false()
        )
        if repository_id is not None:
            file_stmt = file_stmt.where(File.repository_id == repository_id)
        rows = await session.execute(
            file_stmt.order_by(File.path).limit(limit * FILE_CANDIDATE_FACTOR)
        )
        file_records = [
            record
            for record in rows.scalars().all()
            if record.path and not should_exclude_from_retrieval(record.path)
        ]

        symbol_clauses = [Symbol.name.ilike(f"%{kw}%") for kw in keywords] + [
            Symbol.summary.ilike(f"%{kw}%") for kw in keywords
        ]
        symbol_stmt = select(Symbol).where(
            or_(*symbol_clauses) if symbol_clauses else false()
        )
        if repository_id is not None:
            symbol_stmt = symbol_stmt.join(
                File, Symbol.file_id == File.id
            ).where(File.repository_id == repository_id)
        symbol_rows = await session.execute(
            symbol_stmt.limit(limit * SYMBOL_CANDIDATE_FACTOR)
        )
        symbol_records = list(symbol_rows.scalars().all())

    # Ties keep the SQL order (path ascending), so the same query returns the
    # same list twice — a palette that reshuffled under the cursor would be
    # worse than a slow one.
    scored_files = sorted(
        file_records,
        key=lambda record: -score_path_for_keywords(record.path, keywords, text),
    )
    files = [
        {
            "path": record.path,
            "language": record.language,
            "summary": record.summary,
            "file_type": record.file_type,
            "repository_id": record.repository_id,
        }
        for record in scored_files[:limit]
    ]

    scored_symbols = sorted(
        symbol_records,
        key=lambda record: -_symbol_rank(record.name, text, keywords),
    )
    symbols = [
        {
            "name": record.name,
            "kind": record.kind,
            "signature": record.signature,
            "summary": record.summary,
        }
        for record in scored_symbols[:limit]
    ]

    return {"files": files, "symbols": symbols}
