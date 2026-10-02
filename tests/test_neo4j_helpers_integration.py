"""Live Neo4j integration tests for dashboard graph helpers.

Skipped automatically when Neo4j is not reachable (e.g. CI without docker).
"""
import pytest
import pytest_asyncio

from apps.api.helpers import get_neo4j_counts, get_orphan_nodes, get_top_connected_nodes
from brain.database.session import neo4j_driver


async def _neo4j_available() -> bool:
    try:
        await neo4j_driver.verify_connectivity()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture(scope="module")
async def require_neo4j():
    if not await _neo4j_available():
        pytest.skip("Neo4j not available")
    yield


@pytest.mark.asyncio
async def test_top_connected_and_orphans_against_live_neo4j(require_neo4j):
    counts = await get_neo4j_counts()
    if counts["total_nodes"] == 0:
        pytest.skip("Neo4j graph is empty")

    top, top_err = await get_top_connected_nodes(5)
    assert isinstance(top, list)
    assert top_err is None
    assert len(top) > 0
    assert all("name" in n and "label" in n and "degree" in n for n in top)
    assert top[0]["degree"] >= top[-1]["degree"]

    orphans, orphans_err = await get_orphan_nodes(5)
    assert isinstance(orphans, list)
    assert orphans_err is None
