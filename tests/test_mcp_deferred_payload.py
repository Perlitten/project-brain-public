"""Deferred payload delivery for MCP tool results.

What these tests pin down is the contract an agent relies on: a compacted result
is still a usable answer (scalars intact), the discarded tail is reachable by
URI, and nothing is deferred that was small enough to read inline.
"""

import json

import pytest
from mcp.server.fastmcp import FastMCP

from apps.mcp_server.deferred_payload import (
    DEFAULT_LIST_HEAD,
    PAYLOAD_URI_PREFIX,
    DeferredPayloadStore,
    compact_payload,
    inline_budget,
    register_payload_resource,
)


def _fat_impact_payload(files: int = 400) -> dict:
    return {
        "status": "completed",
        "risk": "medium",
        "summary": "Touching the retrieval pipeline reaches the whole search surface.",
        "affected_files": [f"brain/search/module_{index}.py" for index in range(files)],
        "affected_modules": [f"module_{index}" for index in range(files // 4)],
    }


def test_small_payload_travels_inline():
    store = DeferredPayloadStore()
    payload = {"job_id": "job-1", "status": "queued", "status_url": "/jobs/job-1"}

    assert compact_payload(payload, store) == payload


def test_non_dict_payload_is_left_alone():
    store = DeferredPayloadStore()
    answer = "a prose answer that the API already budgeted " * 50

    assert compact_payload(answer, store) == answer


def test_zero_budget_disables_deferral():
    store = DeferredPayloadStore()
    payload = _fat_impact_payload()

    assert compact_payload(payload, store, inline_max_bytes=0) == payload


def test_inline_budget_reads_env_and_ignores_garbage(monkeypatch):
    monkeypatch.setenv("BRAIN_MCP_INLINE_MAX_BYTES", "512")
    assert inline_budget() == 512

    monkeypatch.setenv("BRAIN_MCP_INLINE_MAX_BYTES", "not-a-number")
    assert inline_budget() == 2_000

    monkeypatch.setenv("BRAIN_MCP_INLINE_MAX_BYTES", "-5")
    assert inline_budget() == 0


def test_large_payload_keeps_scalars_and_defers_the_tail():
    store = DeferredPayloadStore()
    payload = _fat_impact_payload()

    compacted = compact_payload(payload, store, inline_max_bytes=2_000)

    # The fields an agent branches on survive verbatim.
    assert compacted["status"] == "completed"
    assert compacted["risk"] == "medium"
    # The enumerations are cut to a head, not emptied.
    assert 0 < len(compacted["affected_files"]) <= DEFAULT_LIST_HEAD
    assert compacted["affected_files"][0] == "brain/search/module_0.py"
    # And the result announces what it dropped and where to read it.
    assert compacted["_full"]["uri"].startswith(PAYLOAD_URI_PREFIX)
    assert compacted["_full"]["omitted_items"] == (400 - len(compacted["affected_files"])) + (
        100 - len(compacted["affected_modules"])
    )
    assert compacted["_full"]["bytes"] > 2_000
    assert len(json.dumps(compacted).encode("utf-8")) <= 2_000


def test_long_strings_are_truncated_not_dropped():
    store = DeferredPayloadStore()
    payload = {"status": "completed", "report": "finding. " * 400}

    compacted = compact_payload(payload, store, inline_max_bytes=600)

    assert compacted["report"].startswith("finding. ")
    assert compacted["report"].endswith("...")
    assert len(compacted["report"]) < len(payload["report"])


def test_deferred_tail_is_readable_as_a_resource():
    store = DeferredPayloadStore()
    payload = _fat_impact_payload()
    compacted = compact_payload(payload, store, inline_max_bytes=2_000)
    token = compacted["_full"]["uri"].removeprefix(PAYLOAD_URI_PREFIX)

    assert json.loads(store.get(token)) == payload


@pytest.mark.asyncio
async def test_resource_template_serves_the_full_payload():
    server = FastMCP("deferred-payload-test")
    store = DeferredPayloadStore()
    register_payload_resource(server, store)
    token = store.put(json.dumps({"affected_files": ["a.py", "b.py"]}))

    templates = {template.uriTemplate for template in await server.list_resource_templates()}
    assert f"{PAYLOAD_URI_PREFIX}{{token}}" in templates

    read = await server.read_resource(f"{PAYLOAD_URI_PREFIX}{token}")
    assert json.loads(list(read)[0].content)["affected_files"] == ["a.py", "b.py"]


@pytest.mark.asyncio
async def test_unknown_token_explains_itself_instead_of_failing():
    server = FastMCP("deferred-payload-test")
    store = DeferredPayloadStore()
    register_payload_resource(server, store)

    read = await server.read_resource(f"{PAYLOAD_URI_PREFIX}deadbeef")
    answer = json.loads(list(read)[0].content)

    assert "no longer held" in answer["error"]
    assert answer["token"] == "deadbeef"


def test_entries_expire_and_capacity_is_bounded():
    now = {"t": 0.0}
    store = DeferredPayloadStore(max_entries=2, ttl_seconds=10.0, clock=lambda: now["t"])

    token = store.put('{"kept": true}')
    now["t"] = 9.0
    assert store.get(token) is not None
    now["t"] = 20.0
    assert store.get(token) is None

    first = store.put('{"n": 1}')
    second = store.put('{"n": 2}')
    third = store.put('{"n": 3}')
    assert store.get(first) is None
    assert store.get(second) is not None
    assert store.get(third) is not None


@pytest.mark.asyncio
async def test_remote_impact_analysis_defers_its_tail(monkeypatch):
    from apps.mcp_server import remote_server

    async def fake_post(_path, _payload, timeout=None):
        return _fat_impact_payload()

    monkeypatch.setattr(remote_server, "_post", fake_post)
    result = await remote_server.impact_analysis("rework the retrieval pipeline")

    assert result["status"] == "completed"
    assert len(result["affected_files"]) <= DEFAULT_LIST_HEAD
    token = result["_full"]["uri"].removeprefix(PAYLOAD_URI_PREFIX)
    assert json.loads(remote_server._PAYLOADS.get(token))["affected_files"] == _fat_impact_payload()["affected_files"]


@pytest.mark.asyncio
async def test_remote_small_result_is_not_deferred(monkeypatch):
    """A wrapped tool whose answer is already small must come back untouched."""
    from apps.mcp_server import remote_server

    small = {"status": "completed", "related_files": ["brain/search/index.py"]}

    async def fake_post(_path, _payload, timeout=None):
        return small

    monkeypatch.setattr(remote_server, "_post", fake_post)
    result = await remote_server.find_related_files("brain/search/retriever.py")

    assert result == small
    assert "_full" not in result


@pytest.mark.asyncio
async def test_both_servers_expose_the_payload_resource():
    from apps.mcp_server import remote_server, server

    for module in (server, remote_server):
        templates = {template.uriTemplate for template in await module.mcp.list_resource_templates()}
        assert f"{PAYLOAD_URI_PREFIX}{{token}}" in templates, module.__name__
