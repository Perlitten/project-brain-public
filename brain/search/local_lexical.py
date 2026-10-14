"""Bounded BM25 companion for the offline token-feature embedding profile."""
from __future__ import annotations

import asyncio
import math
import re
import threading
from collections import Counter
from pathlib import Path

from sqlalchemy import select, func

from brain.config.settings import settings
from brain.database.models import File, FileChunk
from brain.database.session import async_session_factory
from brain.search.code_search import extract_keywords, ENGLISH_STOPWORDS, _IDENTIFIER_PART
from brain.search.filters import should_exclude_from_retrieval


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        tokens.extend(dict.fromkeys([word.lower(), *(part.lower() for part in _IDENTIFIER_PART.findall(word))]))
    return [token for token in tokens if len(token) >= 3 and token not in ENGLISH_STOPWORDS]


def rank_chunks(rows: list[tuple[str, int | None, int | None, str]], query: str, budget: int,
                *, stop: threading.Event | None = None) -> list[dict]:
    terms = sorted(set(extract_keywords(query)))
    if not terms:
        return []
    counts = []
    for _, _, _, content in rows:
        if stop is not None and stop.is_set():
            return []
        counts.append(Counter(_tokens(content)))
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(len(lengths), 1) or 1
    frequency: Counter[str] = Counter()
    for count in counts:
        if stop is not None and stop.is_set():
            return []
        frequency.update(term for term in terms if term in count)
    idf = {term: math.log(1 + (len(rows) - frequency[term] + 0.5) / (frequency[term] + 0.5)) for term in terms}
    best: dict[str, dict] = {}
    for (path, start, end, _), count, length in zip(rows, counts, lengths):
        if stop is not None and stop.is_set():
            return []
        score = sum(idf[term] * count[term] * 2.5 / (count[term] + 1.5 * (0.25 + 0.75 * length / average))
                    for term in terms if count[term])
        if score > best.get(path, {}).get("score", 0):
            best[path] = {"path":path,"start":start,"end":end,"score":score}
    return sorted(best.values(), key=lambda item: (-item["score"], item["path"]))[:budget]


async def local_lexical(repository_id: int | None, query: str, budget: int) -> list[dict]:
    if repository_id is None:
        return []
    async with async_session_factory() as session:
        stmt = select(File.path, FileChunk.start_line, FileChunk.end_line, func.substr(FileChunk.content,1,12_000)).join(
            File, FileChunk.file_id == File.id).where(File.repository_id == repository_id).order_by(
                FileChunk.id).limit(max(1, settings.BRAIN_LITE_MAX_CHUNKS) + 1).execution_options(yield_per=128)
        bounded: list[tuple[str, int | None, int | None, str]] = []
        wants_eval = bool(re.search(r"\b(?:benchmarks?|eval|evaluation|metrics|golden)\b", query, re.I))
        used = count = 0
        async for path,start,end,content in await session.stream(stmt):
            count += 1
            if count > max(1, settings.BRAIN_LITE_MAX_CHUNKS):
                raise ValueError("Offline lexical corpus exceeds configured chunk limit")
            if should_exclude_from_retrieval(path) or (path.startswith("eval/") and not wants_eval):
                continue
            if Path(path).suffix not in {".py", ".ts", ".tsx", ".js", ".kt", ".rs", ".go", ".java"}:
                continue
            used += len(content.encode("utf-8"))
            if used > 8 * 1024 * 1024:
                raise ValueError("Offline lexical input exceeds the 8 MiB work budget")
            bounded.append((path,start,end,content))
    stop = threading.Event()
    try:
        return await asyncio.to_thread(rank_chunks, bounded, query, budget, stop=stop)
    finally:
        stop.set()
