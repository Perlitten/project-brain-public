import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from token_economy_benchmark import _outcome, _pin_check  # noqa: E402
from token_economy_schema import (  # noqa: E402
    TokenEconomyTask,
    is_literal_assertion,
    load_tasks,
)


def test_legacy_curated_corpus_is_explicitly_ineligible_for_production_acceptance():
    tasks, corpus = load_tasks(ROOT / "eval" / "token_economy_tasks.json")

    assert len(tasks) == 50
    assert corpus["acceptance_eligible"] is False
    assert "schema_v2_required" in corpus["acceptance_ineligible_reasons"]
    assert any(reason.endswith(":production_real_required") for reason in corpus["acceptance_ineligible_reasons"])
    assert any(reason.endswith(":expected_symbol_required") for reason in corpus["acceptance_ineligible_reasons"])
    assert any(reason.endswith(":expected_range_required") for reason in corpus["acceptance_ineligible_reasons"])


def test_task_completion_requires_file_symbol_range_and_targeted_read_assertion():
    task = TokenEconomyTask(
        id="production-1",
        question="Locate the API search route",
        expected_files=("apps/api/routers/core.py",),
        expected_symbols=("search_code_endpoint",),
        expected_ranges=(("apps/api/routers/core.py", 540, 560),),
        required_assertions=("Hybrid file/symbol/chunk search",),
        category="known_symbol",
        language="en",
        provenance={"production_real": True},
    )
    outcome = _outcome(
        [{"path": "apps/api/routers/core.py", "symbols": ["search_code_endpoint"], "ranges": [[535, 565]]}],
        [{"path": "apps/api/routers/core.py", "bytes": b"Hybrid file/symbol/chunk search"}],
        task,
    )

    assert outcome["hit_at_3_any"] is True
    assert outcome["evidence_complete"] is True
    assert outcome["terminal_outcome"] == "completed"


def test_literal_classification_is_verbatim_grounded(tmp_path):
    (tmp_path / "mod.py").write_text("def route(rule):\n    return add_url_rule(rule)\n")
    texts = {"mod.py": (tmp_path / "mod.py").read_text()}

    assert is_literal_assertion("def route", texts) is True
    assert is_literal_assertion("add_url_rule(rule)", texts) is True
    assert is_literal_assertion("route decorates view functions.", texts) is False
    assert is_literal_assertion("STALE_IDENTIFIER", texts) is False
    # Without file context only bounded non-sentences are plausible literals.
    assert is_literal_assertion("LANGUAGE_MAP") is True
    assert is_literal_assertion("descriptive prose sentence.") is False


def test_outcome_separates_in_range_misses_from_outside_range(tmp_path):
    (tmp_path / "mod.py").write_text(
        "line1\nIN_RANGE_LITERAL\nline3\nOUTSIDE_RANGE_LITERAL\n"
    )
    task = TokenEconomyTask(
        id="t1",
        question="Where is x?",
        expected_files=("mod.py",),
        expected_symbols=("s1",),
        expected_ranges=(("mod.py", 1, 2),),
        required_assertions=("IN_RANGE_LITERAL", "OUTSIDE_RANGE_LITERAL"),
        category="unknown_location",
        language="en",
        provenance={"production_real": False},
    )
    outcome = _outcome(
        [{"path": "mod.py", "symbols": [], "ranges": [[1, 2]]}],
        [{"path": "mod.py", "bytes": b"line1\nIN_RANGE_LITERAL"}],
        task,
        root=tmp_path,
    )
    assert outcome["literal_assertions_missing"] == {
        "OUTSIDE_RANGE_LITERAL": "outside_expected_ranges"
    }
    assert outcome["literal_assertion_total"] == 2
    assert any(
        r.startswith("literal_assertions_missing") for r in outcome["failure_reasons"]
    )


def test_v2_corpus_is_pinned_and_curated_ineligible():
    tasks, corpus = load_tasks(ROOT / "eval" / "token_economy_tasks_v2.json")

    assert len(tasks) == 50
    assert corpus["repository"]["pinned_commit"]
    assert corpus["acceptance_eligible"] is False
    assert "q001:production_real_required" in corpus[
        "acceptance_ineligible_reasons"
    ]
    assert all(task.provenance.get("production_real") is not True for task in tasks)


def test_flask_corpus_holdout_is_excluded_by_default():
    tasks, corpus = load_tasks(ROOT / "eval" / "token_economy_tasks_flask.json")
    assert len(tasks) == 16
    assert corpus["holdout_excluded"] == ["f017", "f018", "f019", "f020"]

    all_tasks, _ = load_tasks(
        ROOT / "eval" / "token_economy_tasks_flask.json", include_holdout=True
    )
    assert len(all_tasks) == 20


def test_missing_expected_files_do_not_enable_un_grounded_literal_scoring(tmp_path):
    task = TokenEconomyTask(
        id="missing", question="Find the route", expected_files=("missing.py",),
        expected_symbols=("route",), expected_ranges=(("missing.py", 1, 2),),
        required_assertions=("STALE_LITERAL",), category="known_symbol",
        language="en", provenance={"production_real": False},
    )
    outcome = _outcome([], [], task, root=tmp_path)
    assert outcome["literal_assertion_total"] == 0
    assert outcome["unscored_assertion_total"] == 1
    assert is_literal_assertion("", {"mod.py": "text"}) is False


def test_pin_alignment_checks_dirty_expected_files_even_at_pinned_head(tmp_path):
    import subprocess
    from types import SimpleNamespace

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init")
    source = tmp_path / "mod.py"
    source.write_text("def route(): pass\n")
    git("add", "mod.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "pin")
    corpus = {"repository": {"pinned_commit": git("rev-parse", "HEAD")}}
    tasks = [SimpleNamespace(expected_files=("mod.py",))]
    assert _pin_check(tasks, corpus, tmp_path)["alignment_mode"] == "head_equals_pin"
    source.write_text("def changed(): pass\n")
    check = _pin_check(tasks, corpus, tmp_path)
    assert check["aligned"] is False
    assert check["changed_expected_files"] == ["mod.py"]
