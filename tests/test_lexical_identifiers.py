"""Lexical search finds code by the identifiers a query names.

Regression: "HTTPAdapter" did not surface ``requests/adapters.py`` in the file
channel. The keyword ``httpadapter`` is in neither the path nor (necessarily)
the summary; the file is found through its CamelCase words and through the
symbol it defines.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.embeddings.constants import VectorSearchStatus
from brain.search.code_search import extract_keywords, search_code


def test_camel_case_identifier_is_kept_whole_and_split_into_words():
    assert extract_keywords("HTTPAdapter") == ["httpadapter", "http", "adapter"]


def test_snake_case_identifier_is_kept_whole_and_split_into_words():
    assert extract_keywords("http_adapter retries") == ["http_adapter", "http", "adapter", "retries"]


def test_identifier_with_trailing_digits_and_acronyms():
    assert extract_keywords("parseJSON2 getURLPath") == [
        "parsejson2", "parse", "json", "geturlpath", "get", "url", "path",
    ]


def test_plain_words_and_stopword_fallback_are_unchanged():
    assert extract_keywords("Where is the Session class") == ["session", "class"]
    assert extract_keywords("why did they") == ["why", "did"]


def _file(path, summary=""):
    return SimpleNamespace(path=path, language="python", summary=summary, file_type="source")


def _session(file_rows, symbol_rows):
    files = MagicMock()
    files.scalars.return_value.all.return_value = file_rows
    symbols = MagicMock()
    symbols.all.return_value = symbol_rows
    session = AsyncMock()
    session.execute.side_effect = [files, symbols]
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    return factory


@pytest.mark.asyncio
async def test_file_defining_the_named_symbol_leads_the_file_channel():
    adapters = _file("requests/adapters.py", "Transport adapters.")
    # Path hits come back alphabetically; with limit=2 adapters.py would be cut.
    path_rows = [_file("docs/http.md"), _file("requests/_internal_http.py"), adapters]
    symbol = SimpleNamespace(
        name="HTTPAdapter", kind="class", signature="class HTTPAdapter(BaseAdapter)",
        summary="", start_line=120, end_line=480,
    )
    with (
        patch("brain.search.code_search.async_session_factory", _session(path_rows, [(symbol, adapters)])),
        patch(
            "brain.search.code_search.vector_search_chunks",
            AsyncMock(return_value=SimpleNamespace(status=VectorSearchStatus.OK, matches=[])),
        ),
    ):
        result = await search_code("HTTPAdapter", limit=2)

    assert [f["path"] for f in result["files"]] == ["requests/adapters.py", "docs/http.md"]
    assert result["symbols"][0]["path"] == "requests/adapters.py"
