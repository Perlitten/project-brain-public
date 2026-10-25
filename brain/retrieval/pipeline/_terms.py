"""Code-term extraction and the content-BM25 fallback channel."""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from sqlalchemy import select
from brain.database.models import File, FileChunk
from brain.database.session import async_session_factory
from brain.retrieval.types import ChannelCandidate


_IDENT_RE = re.compile(
    r"\b(?:[a-z][a-z0-9]*_[a-z0-9_]{2,}|[a-z]+[A-Z][a-zA-Z0-9]*|[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+)\b"
)


# File paths are mined without a backtracking regex: candidate tokens are cut on
# characters that cannot appear in a path, then checked for shape in plain code.
_PATH_TOKEN_RE = re.compile(r"[\w./-]+")


_SOURCE_FILE_EXTENSIONS = frozenset({
    "py", "pyi", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "h", "hpp", "cpp", "rb",
    "md", "rst", "txt", "html", "css", "toml", "yaml", "yml", "json",
})


def _extract_file_paths(text: str) -> List[str]:
    """Return ``dir/file.ext`` tokens: at least one directory segment and a known extension."""
    paths: List[str] = []
    for token in _PATH_TOKEN_RE.findall(text):
        candidate = token.rstrip("./-")
        if "/" not in candidate:
            continue
        segments = [segment for segment in candidate.split("/") if segment]
        if len(segments) < 2:
            continue
        name = segments[-1]
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem or ext not in _SOURCE_FILE_EXTENSIONS:
            continue
        paths.append(candidate)
    return paths


_ERROR_NAME_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]*(?:Error|Exception|Warning|Fault|Timeout|NotFound)\b")


_STACK_FRAME_RE = re.compile(r'File "([^"]+)", line \d+')


_ID_QUERY_STOPWORDS = frozenset({
    "__init__", "__main__", "__name__", "self.args", "self.assert", "self.assertEqual",
    "None.None", "True.False",
})


def extract_code_query_terms(text: str, limit: int = 24) -> List[str]:
    """Mine a task description for code-shaped terms: file paths, stack frames,
    CamelCase/snake_case/dotted identifiers, and *Error/*Exception names.
    Feeds the lexical/symbol/hints channels so issue text becomes a code query."""
    import keyword

    terms: List[str] = []
    terms.extend(_extract_file_paths(text))
    terms.extend(_STACK_FRAME_RE.findall(text))
    terms.extend(_ERROR_NAME_RE.findall(text))
    terms.extend(_IDENT_RE.findall(text))
    seen: set = set()
    out: List[str] = []
    for term in terms:
        norm = term.strip().lower()
        if len(norm) < 4 or norm in seen or norm in _ID_QUERY_STOPWORDS:
            continue
        head = norm.split(".")[-1]
        if keyword.iskeyword(head) or keyword.iskeyword(norm):
            continue
        seen.add(norm)
        out.append(term.strip())
        if len(out) >= limit:
            break
    return out


_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _bm25_tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


async def _content_bm25_channel(
    repository_id: Optional[int],
    query: str,
    top_n: int = 50,
) -> List[ChannelCandidate]:
    """Content-level BM25 over chunk text — the fallback channel for corpora
    where path/symbol lexical channels cannot fire (anonymous snippets)."""
    async with async_session_factory() as session:
        stmt = select(FileChunk.content, File.path).join(File, FileChunk.file_id == File.id)
        if repository_id is not None:
            stmt = stmt.where(File.repository_id == repository_id)
        rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    docs = [_bm25_tokenize(r[0]) for r in rows]
    q = list(dict.fromkeys(_bm25_tokenize(query)))
    if not q:
        return []
    k1, b = 1.5, 0.75
    n_docs = len(docs)
    df: Dict[str, int] = {}
    tfs: List[Dict[str, int]] = []
    total_len = 0
    for d in docs:
        tf: Dict[str, int] = {}
        for tok in d:
            tf[tok] = tf.get(tok, 0) + 1
        tfs.append(tf)
        total_len += len(d)
        for tok in tf:
            df[tok] = df.get(tok, 0) + 1
    avg_len = total_len / max(n_docs, 1) or 1.0
    import math

    idf = {w: math.log(1.0 + (n_docs - df.get(w, 0) + 0.5) / (df.get(w, 0) + 0.5)) for w in q}
    best_by_path: Dict[str, float] = {}
    for tf, (_content, path) in zip(tfs, rows):
        dl = len(tf) or 1
        s = 0.0
        for w in q:
            f = tf.get(w)
            if not f:
                continue
            s += idf[w] * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avg_len))
        if s > best_by_path.get(path, 0.0):
            best_by_path[path] = s
    ranked = sorted(best_by_path.items(), key=lambda kv: -kv[1])[:top_n]
    return [
        ChannelCandidate("bm25", path, score, rank=i)
        for i, (path, score) in enumerate(ranked, 1)
    ]
