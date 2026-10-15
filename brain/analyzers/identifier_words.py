"""Split identifiers into words without a regular expression.

``HTTPAdapter`` -> ``["HTTP", "Adapter"]``, ``parseJSON2`` -> ``["parse", "JSON"]``,
``http_adapter`` -> ``["http", "adapter"]``. An acronym run ends where a capitalised
word starts; digits, underscores and any other non-letter are separators. The scan
is a single left-to-right pass, so it stays linear on attacker-shaped input.
"""
from __future__ import annotations

from typing import List


def _is_upper(char: str) -> bool:
    return "A" <= char <= "Z"


def _is_lower(char: str) -> bool:
    return "a" <= char <= "z"


def split_identifier_words(text: str) -> List[str]:
    """Return the ASCII-letter words of ``text`` in order of appearance."""
    words: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if _is_upper(char):
            end = index + 1
            while end < length and _is_upper(text[end]):
                end += 1
            if end < length and _is_lower(text[end]):
                # "HTTPAdapter": the acronym keeps all but its last capital, which
                # starts the capitalised word that follows.
                if end - index >= 2:
                    words.append(text[index:end - 1])
                    index = end - 1
                while end < length and _is_lower(text[end]):
                    end += 1
            words.append(text[index:end])
            index = end
        elif _is_lower(char):
            end = index + 1
            while end < length and _is_lower(text[end]):
                end += 1
            words.append(text[index:end])
            index = end
        else:
            index += 1
    return words
