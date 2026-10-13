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


"""Tracked-content scan: banned phrases must not come back.

The denylist is stored as SHA-256 digests of normalized (lowercase,
alnum-tokenized) phrases — the private names themselves never appear in the
repo, not even as "banned" literals. Scanner tokenizes each file the same way
and hashes word 1..3-grams, so separators (``-``, ``_``, ``/``, ``.``) cannot
dodge it. Extra phrases can be injected via the ``PRIVACY_DENYLIST`` env var
(a path to a file with one phrase per line, or a comma/newline-separated
inline list) so CI can extend the list from a secret without committing it.
"""

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MAX_GRAM = 3

# This exact owner/repository is publicly reachable. Recognize structured
# references, not arbitrary occurrences of the same words or private URLs.
_PUBLIC_REPOSITORY_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:https://(?:github\.com|raw\.githubusercontent\.com)/)?"
    r"Perlitten/project-brain-public(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

# sha256 digests of normalized banned phrases (e.g. "foo-bar" -> "foo bar").
# Digests only: the phrases they stand for are private by definition.
_HASHED_DENYLIST = frozenset(
    [
        "f9ae6029616303ab8986bb9e032d0f4d6fe9364e1d5b98633b224de3d0c25556",
        "5b5a0738deda6137430dfb995213df2e410b300af405a134809b0f22f33836fa",
        "fe4b58fe2c7f3aa98f98446e7703e4ba0a53600661ebc0767cb220c9e8933b0a",
        "156dfb7de8e57b949606c76233aa183cb06b6cd856fcd212778179e78c58dfcf",
        "59e4a42b6dafb7f7fb6fffdb07065929fc1ecbaafc1871f0a69160d09c0e02cc",
        "c99f9097fa6bc5aa4b12830201aa7a312d6dc8ac43930989574ce235a4797429",
        "2bd86d1eb4193599fdf236c17f65e52ef6b47b8077f07e1ed35f8c7cc830badc",
        "e835aab906a853fc2fc2bf37b2c599dd6cd215c314a1b5980e1b7ce2fbfccc90",
        "738ce83102b9af780be97f1aa9a910e2977fd951a14e7651ce9984c5744bf571",
        "40b33da52694aac15056649fea999eae410fed5628485030c93ed4e5b6ea46c7",
        "089e0eaea163722756c931fc9ee8bf795f8f14ccfc00d519fbfa5ae8abfe72c5",
        "636396f02b6571e40d8fe91cba550515c0cdc0e7d314c210e00b02256375a796",
    ]
)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _digest_tokens(tokens: list[str]) -> str:
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest()


def _denylist_hashes() -> frozenset[str]:
    hashes = set(_HASHED_DENYLIST)
    raw = os.environ.get("PRIVACY_DENYLIST", "").strip()
    if raw:
        if Path(raw).is_file():
            raw = Path(raw).read_text(encoding="utf-8")
        for phrase in re.split(r"[\n,;]", raw):
            phrase = phrase.strip()
            if phrase:
                hashes.add(_digest_tokens(_tokens(phrase)))
    return frozenset(hashes)


# digest -> tracked files allowed to carry it (never an owner leak there).
_ALLOWLIST: dict[str, tuple[str, ...]] = {
    # eval fixture that legitimately demonstrates a generic home path
    "2bd86d1eb4193599fdf236c17f65e52ef6b47b8077f07e1ed35f8c7cc830badc": (
        "eval/harness_benchmark/tasks/memory_004_many_learnings.json",
    ),
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.split()


def _scan_file(path: str, denylist: frozenset[str] | None = None):
    deny = denylist if denylist is not None else _denylist_hashes()
    try:
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return  # binary or unreadable — nothing text-shaped to leak
    allowed = {h for h, files in _ALLOWLIST.items() if path in files}
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Replace only the canonical public reference. Keep every denylist
        # digest and continue scanning the URL's remaining path/query text.
        tokens = _tokens(_PUBLIC_REPOSITORY_REFERENCE.sub("public_repository", line))
        for n in range(1, min(_MAX_GRAM, len(tokens)) + 1):
            for i in range(len(tokens) - n + 1):
                d = _digest_tokens(tokens[i : i + n])
                if d in deny and d not in allowed:
                    yield f"{path}:{lineno}: denylist_hit_{d[:8]}"


def test_tree_has_no_banned_patterns():
    deny = _denylist_hashes()
    hits = [hit for path in _tracked_files() for hit in _scan_file(path, deny)]
    assert hits == [], "denylist hit:\n" + "\n".join(hits[:20])


def test_scanner_catches_banned_text(tmp_path):
    synthetic = "zzsynthetic banned-phrase"
    deny = frozenset({_digest_tokens(_tokens(synthetic))})
    sample = REPO_ROOT / "tests" / "_banned_probe.tmp"
    sample.write_text("see zzSynthetic BANNED phrase for details", encoding="utf-8")
    try:
        hits = list(_scan_file(sample.relative_to(REPO_ROOT).as_posix(), deny))
        assert hits and hits[0].startswith("tests/_banned_probe.tmp:")
    finally:
        sample.unlink(missing_ok=True)


def test_env_denylist_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVACY_DENYLIST", "another-synthetic phrase")
    deny = _denylist_hashes()
    assert _digest_tokens(["another", "synthetic", "phrase"]) in deny


def test_exact_public_identity_does_not_allow_private_or_unqualified_text(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    public_identity = "Perlitten/project-brain-public"
    sample = tmp_path / "sample.md"
    sample.write_text(f"https://github.com/{public_identity}/issues\nBRAIN_GITHUB_REPOSITORY={public_identity}\n")
    assert not list(_scan_file("sample.md"))
    # Previously README's file-wide exemption would admit this too. It now
    # remains banned outside an exact, known-public owner/repository reference.
    (tmp_path / "README.md").write_text(public_identity.split("/", 1)[1])
    assert list(_scan_file("README.md"))
    sample.write_text("https://example.invalid/" + public_identity)
    assert list(_scan_file("sample.md"))


def test_public_reference_does_not_hide_banned_url_suffix(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    public_url = "https://github.com/Perlitten/project-brain-public"
    synthetic = "zzsynthetic banned phrase"
    deny = frozenset({_digest_tokens(_tokens(synthetic))})
    (tmp_path / "sample.md").write_text(public_url + "/zzsynthetic-banned-phrase")
    assert list(_scan_file("sample.md", deny))
