"""The release gate must find metadata regressions in actual Git objects."""

import os
import subprocess

from scripts.check_public_privacy import audit_history


def _commit(
    repo, *, author_email="61577193+Perlitten@users.noreply.github.com",
    committer_email="61577193+Perlitten@users.noreply.github.com", message="Public change",
):
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Perlitten",
        "GIT_COMMITTER_NAME": "Perlitten",
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_EMAIL": committer_email,
    }
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message],
        check=True,
        capture_output=True,
        env=env,
    )


def test_public_identity_passes(tmp_path):
    _commit(tmp_path)
    assert audit_history(tmp_path)["pass"] is True


def test_normal_github_merge_is_allowed_only_in_ci_mode(tmp_path):
    _commit(tmp_path, committer_email="noreply@github.com")
    assert audit_history(tmp_path)["pass"] is False
    assert audit_history(tmp_path, allow_github_identities=True)["pass"] is True


def test_private_author_fails_without_echoing_address(tmp_path):
    address = "synthetic-person@example.org"
    _commit(tmp_path, author_email=address)
    report = audit_history(tmp_path)
    assert report["pass"] is False
    assert report["issues"][0]["issue"] == "unexpected_author_identity"
    assert address not in str(report)
    assert audit_history(tmp_path, allow_github_identities=True)["pass"] is False


def test_personal_email_footer_fails_without_echoing_address(tmp_path):
    address = "synthetic-person@gmail.com"
    _commit(tmp_path, message=f"Public change\n\nCo-authored-by: Synthetic <{address}>")
    report = audit_history(tmp_path)
    assert report["pass"] is False
    assert report["issues"][0]["issue"] == "personal_email_in_message"
    assert address not in str(report)
