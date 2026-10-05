"""Tests for the diff-review LLM fallback behaviour.

Regression tests for the bug where an unparseable LLM response silently
degraded to "No suspicious or unrelated changes identified." — i.e. a diff
that was never actually reviewed looked approved.
"""

from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.analyzers.diff_analyzer import (
    DiffAnalyzer,
    _parse_llm_json_response,
)


# --- _parse_llm_json_response unit tests ------------------------------------


def test_parse_plain_json():
    payload = '{"rule_violations": [], "suspicious_changes": [], "feedback": "ok"}'
    assert _parse_llm_json_response(payload)["feedback"] == "ok"


def test_parse_fenced_json():
    payload = '```json\n{"feedback": "ok"}\n```'
    assert _parse_llm_json_response(payload)["feedback"] == "ok"


def test_parse_json_with_surrounding_prose():
    payload = 'Here is the review:\n{"feedback": "ok"}\nHope this helps.'
    assert _parse_llm_json_response(payload)["feedback"] == "ok"


def test_parse_repairs_invalid_escapes():
    # LLMs often emit raw backslashes (Windows paths, regexes). This used to
    # raise "Invalid \\escape" and silently skip the whole review.
    payload = '{"rule_violations": [], "suspicious_changes": [], "feedback": "regex \\d+ failed at C:\\Prod"}'
    assert _parse_llm_json_response(payload)["feedback"] == "regex \\d+ failed at C:\\Prod"


def test_parse_garbage_raises():
    with pytest.raises(Exception):
        _parse_llm_json_response("definitely not json at all")


# --- review_diff fallback behaviour ------------------------------------------


def _ok_git(args, text=False):
    cmd = " ".join(args)
    if "rev-parse --is-inside-work-tree" in cmd:
        return CompletedProcess(args=args, returncode=0, stdout="true\n", stderr="")
    if "diff --name-only" in cmd:
        return CompletedProcess(args=args, returncode=0, stdout="brain/foo.py\n", stderr="")
    if cmd.startswith("git diff "):
        return CompletedProcess(args=args, returncode=0, stdout="diff --git a/brain/foo.py\n", stderr="")
    if "rev-parse HEAD" in cmd:
        return CompletedProcess(args=args, returncode=0, stdout="abc123\n", stderr="")
    raise AssertionError(f"unexpected git call: {cmd}")


def _make_session_mock():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_analyzer(tmp_path, llm_generate):
    analyzer = DiffAnalyzer(tmp_path)
    analyzer._run_git = AsyncMock(side_effect=_ok_git)
    fake_llm = SimpleNamespace(generate=AsyncMock(side_effect=llm_generate))
    analyzer.router = SimpleNamespace(llm=lambda kind: fake_llm)
    return analyzer


@pytest.mark.asyncio
async def test_failed_llm_review_is_never_reported_as_clean(tmp_path):
    async def boom(**kwargs):
        raise RuntimeError("provider exploded")

    analyzer = _make_analyzer(tmp_path, boom)
    with (
        patch("brain.analyzers.diff_analyzer.RuleStore.list_active_rules", new=AsyncMock(return_value=[])),
        patch("brain.analyzers.diff_analyzer.async_session_factory", new=_make_session_mock()),
    ):
        result = await analyzer.review_diff(base="HEAD~1", head="HEAD")

    assert result["llm_review_status"] == "failed"
    # An unchecked diff must not look approved.
    assert result["status"] == "needs_review"
    md = result["markdown_content"]
    assert "were not checked" in md or "did not run" in md
    assert "No suspicious or unrelated changes identified" not in md
    assert "No rule violations detected" not in md


@pytest.mark.asyncio
async def test_invalid_escape_json_is_repaired_not_dropped(tmp_path):
    async def bad_escapes(**kwargs):
        return '{"rule_violations": [], "suspicious_changes": ["debug print left in D:\\prod\\data"], "feedback": "looks fine"}'

    analyzer = _make_analyzer(tmp_path, bad_escapes)
    with (
        patch("brain.analyzers.diff_analyzer.RuleStore.list_active_rules", new=AsyncMock(return_value=[])),
        patch("brain.analyzers.diff_analyzer.async_session_factory", new=_make_session_mock()),
    ):
        result = await analyzer.review_diff(base="HEAD~1", head="HEAD")

    assert result["llm_review_status"] == "completed"
    assert result["suspicious_changes"] == ["debug print left in D:\\prod\\data"]
    assert result["status"] == "needs_review"
