"""Scheduled jobs and the merge webhook default to the indexed repository."""

from brain.config.settings import Settings


def test_blank_repo_paths_follow_target_repo(tmp_path):
    s = Settings(TARGET_REPO_PATH=str(tmp_path), NIGHTLY_DEEP_MAINTENANCE_REPO_PATH="", PROJECT_BRAIN_GIT_REPO_PATH="")
    assert s.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH == str(tmp_path)
    assert s.PROJECT_BRAIN_GIT_REPO_PATH == str(tmp_path)


def test_explicit_repo_paths_win(tmp_path):
    s = Settings(TARGET_REPO_PATH=str(tmp_path), NIGHTLY_DEEP_MAINTENANCE_REPO_PATH="/srv/a", PROJECT_BRAIN_GIT_REPO_PATH="/srv/b")
    assert s.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH == "/srv/a"
    assert s.PROJECT_BRAIN_GIT_REPO_PATH == "/srv/b"
