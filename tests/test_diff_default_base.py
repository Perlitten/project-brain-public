from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import AsyncMock, patch

import pytest

from brain.analyzers.diff_analyzer import DiffAnalyzer


@pytest.mark.asyncio
async def test_git_commands_trust_only_the_selected_repository(tmp_path):
    analyzer = DiffAnalyzer(tmp_path)
    completed = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("brain.analyzers.diff_analyzer.subprocess.run", return_value=completed) as run:
        result = await analyzer._run_git(["git", "status", "--short"], text=True)

    assert result is completed
    command = run.call_args.args[0]
    assert command == [
        "git",
        "-c",
        f"safe.directory={tmp_path.resolve().as_posix()}",
        "status",
        "--short",
    ]
    assert run.call_args.kwargs["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_default_diff_base_uses_remote_head(tmp_path):
    analyzer = DiffAnalyzer(tmp_path)
    analyzer._run_git = AsyncMock(
        return_value=CompletedProcess(
            args=[],
            returncode=0,
            stdout="origin/master\n",
            stderr="",
        )
    )

    assert await analyzer._resolve_default_base() == "origin/master"
    analyzer._run_git.assert_awaited_once_with(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        text=True,
    )


@pytest.mark.asyncio
async def test_default_diff_base_falls_back_to_head_without_remote(tmp_path):
    analyzer = DiffAnalyzer(tmp_path)
    analyzer._run_git = AsyncMock(
        side_effect=CalledProcessError(returncode=1, cmd=["git", "symbolic-ref"])
    )

    assert await analyzer._resolve_default_base() == "HEAD"


@pytest.mark.asyncio
async def test_review_diff_resolves_omitted_base(tmp_path):
    analyzer = DiffAnalyzer(tmp_path)
    analyzer._resolve_default_base = AsyncMock(return_value="origin/master")
    analyzer._run_git = AsyncMock(
        side_effect=[
            CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]
    )

    result = await analyzer.review_diff(base=None, head="current")

    assert result["status"] == "completed"
    assert result["modified_files"] == []
    analyzer._resolve_default_base.assert_awaited_once()
    assert analyzer._run_git.await_args_list[1].args[0] == [
        "git",
        "diff",
        "--name-only",
        "origin/master",
    ]
