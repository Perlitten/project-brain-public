"""Stable ordering of symbol evidence before bounded SQL selection."""
from sqlalchemy import case, func, literal, or_

from brain.database.models import File, Symbol
from brain.search.filters import FILE_TYPE_WEIGHTS


def symbol_relevance_order(terms: list[str]):
    """Prefer matching names/paths and apply the existing structural weights.

    Natural-language plurals also match singular identifiers. A term contributes
    once regardless of how often it occurs in a name or path.
    """
    name_hits = []
    path_hits = []
    for term in dict.fromkeys(term.casefold() for term in terms):
        variants = [term]
        if len(term) > 4 and term.endswith("s"):
            variants.append(term[:-1])
        name_hits.append(case((or_(*(Symbol.name.ilike(f"%{v}%") for v in variants)), 1), else_=0))
        path_hits.append(case((or_(*(File.path.ilike(f"%{v}%") for v in variants)), 1), else_=0))
    weight = case(FILE_TYPE_WEIGHTS, value=File.file_type, else_=0.7)
    return (
        (sum(name_hits, literal(0)) * weight).desc(),
        (sum(path_hits, literal(0)) * weight).desc(),
        weight.desc(),
        File.path,
        func.lower(Symbol.name),
        Symbol.id,
    )
