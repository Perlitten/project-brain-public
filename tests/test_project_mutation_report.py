"""The campaign must not turn partial runs, crashes or missing modules into a pass."""

import ast
import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eval.project_mutation import assign_shards, candidate_hash, outcome, prepare_git_pointer, subprocess_guard, summarize
from eval.mutation_types import build_type_proof, read_type_exclusions


def module_record(tmp_path, codes):
    source = tmp_path / "example.py"
    source.write_text("pass\n")
    (tmp_path / "example.py.meta").write_text(json.dumps({"exit_code_by_key": codes}))
    return {"module": "example.py", "candidates": len(codes), "candidate_keys_sha256": candidate_hash(codes),
            "generated_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def test_sharding_preserves_the_entire_inventory_and_is_reproducible():
    modules = [{"module": f"brain/{name}.py", "candidates": count}
               for name, count in (("auth", 101), ("store", 70), ("cli", 25), ("models", 0))]
    first = assign_shards(modules, 3)
    assert assign_shards(list(reversed(modules)), 3) == first
    flattened = [row for shard in first for row in shard]
    assert sorted(flattened, key=lambda row: row["module"]) == sorted(modules, key=lambda row: row["module"])
    assert sum(row["candidates"] for row in flattened) == 196
    with pytest.raises(ValueError, match="positive"):
        assign_shards(modules, 0)


def test_generated_git_pointer_survives_repeated_preparation_with_readonly_packfiles(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "example.py").write_text("value = 17\n")
    for command in (["add", "example.py"],
                    ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture"],
                    ["repack", "-ad"]):
        subprocess.run(["git", *command], cwd=tmp_path, check=True, capture_output=True)
    packs = list((tmp_path / ".git/objects/pack").glob("*.pack"))
    assert packs and all(path.stat().st_mode & 0o222 == 0 for path in packs)
    prepare_git_pointer(tmp_path)
    prepare_git_pointer(tmp_path)
    shadow = tmp_path / "mutants"
    assert (shadow / ".git").is_file()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=shadow) == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
    )
    assert Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=shadow, text=True).strip()) == shadow
    (shadow / ".git").unlink()
    (shadow / ".git").mkdir()
    marker = shadow / ".git/keep"
    marker.write_text("existing metadata")
    with pytest.raises(ValueError, match="Preserve the existing"):
        prepare_git_pointer(tmp_path)
    assert marker.read_text() == "existing metadata"


def test_report_keeps_timeouts_in_denominator_and_rejects_incomplete_scores(tmp_path):
    codes = {f"brain.example.f__mutmut_{index}": code
             for index, code in enumerate((1, 1, 0, 36, 3, None))}
    module = module_record(tmp_path, codes)
    path = tmp_path / "example.py.meta"
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    report = summarize([module], tmp_path)["overall"]
    assert report["killed"] == 2 and report["timed_out"] == 1
    assert report["inconclusive"] == 1 and report["pending"] == 1
    assert not report["complete"] and report["score"] is None

    codes["brain.example.f__mutmut_4"] = 0
    codes["brain.example.f__mutmut_5"] = 1
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    report = summarize([module], tmp_path)["overall"]
    assert report["complete"] and report["score"] == 0.5
    assert report["total"] == 6 and report["timed_out"] == 1
    assert report["equivalent"] == report["type_invalid"] == 0
    filtered = summarize([module], tmp_path, {"brain.example.f__mutmut_0": [{"code": "arg-type"}]})["overall"]
    assert filtered["type_invalid"] == 1 and filtered["killed"] == 2
    assert filtered["total"] == 6 and filtered["score"] == 0.4

    path.unlink()
    missing = summarize([module], tmp_path)["overall"]
    assert missing["total"] == missing["pending"] == 6
    assert not missing["complete"] and missing["score"] is None


def test_report_rejects_changed_candidate_identity_and_noninteger_exit_codes(tmp_path):
    codes = {"brain.example.f__mutmut_1": 1}
    module = module_record(tmp_path, codes)
    (tmp_path / "example.py.meta").write_text(json.dumps({"exit_code_by_key": {"different": 1}}))
    with pytest.raises(ValueError, match="identities changed"):
        summarize([module], tmp_path)
    (tmp_path / "example.py.meta").write_text(json.dumps({"exit_code_by_key": codes}))
    (tmp_path / "example.py").write_text("different_body = True\n")
    with pytest.raises(ValueError, match="Generated source changed"):
        summarize([module], tmp_path)
    with pytest.raises(ValueError, match="integers"):
        outcome(True)
    with pytest.raises(ValueError, match="integers"):
        outcome("1")
    assert outcome(37) == outcome(3) == outcome(-11) == "inconclusive"


def test_type_proof_separates_compiler_errors_from_constructor_cloning(tmp_path):
    generated = tmp_path / "generated"
    (generated / "brain").mkdir(parents=True)
    source = generated / "brain/example.py"
    source.write_text('''class Example:
    def __init__(self) -> None:
        self.value: int = 1
    def x_Example_init__mutmut_orig(self) -> None:
        self.value: int = 1

def x_length__mutmut_orig(text: str) -> int:
    return len(text)

def x_length__mutmut_1(text: str) -> int:
    return len(None)
''')
    key = "brain.example.x_length__mutmut_1"
    meta = Path(str(source) + ".meta")
    meta.write_text(json.dumps({"exit_code_by_key": {key: None}}))
    spans = {node.name: [node.lineno, node.end_lineno] for node in ast.walk(ast.parse(source.read_text()))
             if isinstance(node, ast.FunctionDef) and "__mutmut_" in node.name}
    Path(str(source) + ".spans").write_text(json.dumps({"spans": spans}))
    inventory = {"base": "frozen-test-source", "modules": [{"module": "brain/example.py", "sha256": "original",
                 "generated_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}
    checked = subprocess.run(
        [sys.executable, "-m", "mypy", "--follow-imports=skip", "--output=json", "brain/example.py"],
        cwd=generated, text=True, capture_output=True, timeout=30,
    )
    assert checked.returncode == 1, checked.stderr
    diagnostics = tmp_path / "mypy.jsonl"
    diagnostics.write_text(checked.stdout)
    proof = build_type_proof(inventory, generated, diagnostics)
    row = proof["modules"][0]
    assert row["eligible"] and not row["unmapped_errors"]
    assert row["ignored_clone_diagnostics"]
    assert set(row["invalid_candidates"]) == {key}
    saved = tmp_path / "proof.json.gz"
    saved.write_bytes(gzip.compress(json.dumps(proof).encode()))
    assert set(read_type_exclusions(saved, inventory["modules"])) == {key}
    inventory["modules"][0]["generated_sha256"] = "changed"
    with pytest.raises(ValueError, match="does not match source"):
        read_type_exclusions(saved, inventory["modules"])


def test_stats_follow_cli_subprocesses_without_changing_their_working_directory(tmp_path):
    pytest.importorskip("mutmut", reason="optional mutation engine; exercised in the mutation workflow")
    campaign = tmp_path / "campaign"
    (campaign / "brain").mkdir(parents=True)
    (campaign / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["brain"]\nmax_stack_depth = -1\n')
    target = tmp_path / "another-repository"
    target.mkdir()
    stats = tmp_path / "child-stats"
    support = Path(__file__).resolve().parents[1] / "eval/mutation_support"
    package = campaign / "mutants/brain"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    code = '''
import json
import os
from mutmut.mutation.trampoline import wrap_in_trampoline
def x_value__mutmut_orig():
    return 17
def x_value__mutmut_1():
    return 18
@wrap_in_trampoline({"_mutmut_orig": x_value__mutmut_orig, "x_value__mutmut_1": x_value__mutmut_1})
def value():
    return 17
print(json.dumps({"cwd": os.getcwd(), "value": value()}))
'''
    script = package / "example.py"
    script.write_text(code)
    env = {**os.environ, "PYTHONPATH": str(campaign / "mutants"),
           "MUTANT_UNDER_TEST": "stats", "BRAIN_MUTATION_CONFIG_ROOT": str(campaign),
           "BRAIN_MUTATION_SUPPORT_PATH": str(support),
           "BRAIN_MUTATION_CHILD_STATS": str(stats), "PYTEST_CURRENT_TEST": "tests/test_cli.py::test_index (call)"}
    env.pop("BRAIN_MUTATION_ORIGINAL_MUTANT", None)
    with subprocess_guard():
        result = subprocess.run(
            [sys.executable, "-m", "brain.example"], cwd=target, text=True, capture_output=True, timeout=20, env=env,
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"cwd": str(target), "value": 17}
    records = [json.loads(path.read_text()) for path in stats.glob("*.json")]
    assert len(records) == 1
    assert records[0]["test"] == "tests/test_cli.py::test_index"
    assert "brain.example.x_value" in records[0]["functions"]
    # A directly invoked script runs as __main__. The named package mutant must
    # still execute rather than falsely surviving because its module differs.
    env["MUTANT_UNDER_TEST"] = "brain.example.x_value__mutmut_1"
    with subprocess_guard():
        mutated = subprocess.run(
            [sys.executable, str(script)], cwd=target, text=True, capture_output=True, timeout=20, env=env,
        )
    assert mutated.returncode == 0, mutated.stderr
    assert json.loads(mutated.stdout) == {"cwd": str(target), "value": 18}
    # The installed hook must not alter a normal provider process. This uses a
    # synthetic value and makes no provider request.
    ordinary_env = {key: value for key, value in os.environ.items()
                    if not key.startswith("BRAIN_MUTATION_") and key not in {"PYTHONPATH", "MUTANT_UNDER_TEST"}}
    ordinary_env["NVIDIA_API_KEY"] = "synthetic-not-a-provider-key"
    with subprocess_guard():
        ordinary = subprocess.run(
            [sys.executable, "-c", 'import os; assert os.environ["NVIDIA_API_KEY"] == "synthetic-not-a-provider-key"'],
            cwd=target, env=ordinary_env, text=True, capture_output=True, timeout=20,
        )
    assert ordinary.returncode == 0, ordinary.stderr


def test_aggregate_recomputes_raw_scores_and_requires_matching_complete_evidence(tmp_path):
    from copy import deepcopy
    from eval.project_mutation_aggregate import aggregate

    modules = []
    raw = {}
    for name, codes in (("brain/security.py", {"brain.security.x_check__mutmut_1": 1,
                                              "brain.security.x_check__mutmut_2": 0}),
                        ("brain/store.py", {"brain.store.x_write__mutmut_1": 36,
                                           "brain.store.x_write__mutmut_2": 37})):
        modules.append({"module": name, "candidates": 2, "sha256": "original-" + name,
                        "generated_sha256": "generated-" + name, "candidate_keys_sha256": candidate_hash(codes)})
        raw[name] = codes
    inventory = {"base": "frozen-base", "tool": "mutmut==3.8.0", "modules": modules}
    reports = [{"base": inventory["base"], "tool": inventory["tool"], "shard": index, "shard_count": 2,
                "type_proof_sha256": "compiler-proof", "modules": shard,
                "exit_code_by_key": {key: code for row in shard for key, code in raw[row["module"]].items()},
                "verified": True, "runner_exit_code": 0, "measurement_exit_code": 0, "error": None,
                "seconds": 5, "test_sha256": {"tests/test_contract.py": "identical-tests"},
                "overall": {"score": 1.0, "killed": 9999}}  # Stored scores are deliberately false.
               for index, shard in enumerate(assign_shards(modules, 2))]
    invalid = {"brain.store.x_write__mutmut_2": [{"code": "arg-type"}]}
    decision = {"brain.security.x_check__mutmut_2": {
        "classification": "equivalent", "reason": "Identical accepted encoding alias",
        "source_sha256": "original-brain/security.py"}}
    combined = aggregate(inventory, list(reversed(reports)), invalid, "compiler-proof", decision)
    assert combined["overall"]["complete"] and combined["overall"]["score"] == 0.5
    assert {key: combined["overall"][key] for key in ("killed", "timed_out", "equivalent", "type_invalid")} == {
        "killed": 1, "timed_out": 1, "equivalent": 1, "type_invalid": 1}
    assert combined["worker_seconds"] == 10
    assert combined["unclassified_survivors"] == []
    missing = aggregate(inventory, reports[:1], invalid, "compiler-proof", decision)
    assert not missing["overall"]["complete"] and missing["overall"]["score"] is None
    unreviewed = aggregate(inventory, reports, invalid, "compiler-proof")
    assert unreviewed["overall"]["score"] == 1 / 3
    assert unreviewed["unclassified_survivors"] == list(decision)
    with pytest.raises(ValueError, match="Duplicate shard"):
        aggregate(inventory, reports + reports[:1], invalid, "compiler-proof", decision)
    for field, replacement, expected in (
        ("test_sha256", {"changed-test": "different"}, "test fingerprints"),
        ("type_proof_sha256", "unproven", "compiler proof"),
        ("exit_code_by_key", {"brain.unrelated.x__mutmut_1": 1}, "candidate identities"),
    ):
        changed = deepcopy(reports)
        changed[0][field] = replacement
        with pytest.raises(ValueError, match=expected):
            aggregate(inventory, changed, invalid, "compiler-proof", decision)
    incomplete = deepcopy(reports)
    incomplete[0]["verified"] = False
    assert aggregate(inventory, incomplete, invalid, "compiler-proof", decision)["overall"]["score"] is None
    decision["brain.security.x_check__mutmut_2"]["source_sha256"] = "different-source"
    with pytest.raises(ValueError, match="bound to the frozen source"):
        aggregate(inventory, reports, invalid, "compiler-proof", decision)


def test_changing_module_scope_rebuilds_full_statistics_instead_of_claiming_no_tests(tmp_path):
    pytest.importorskip("mutmut", reason="optional mutation engine; exercised in the mutation workflow")
    import shutil

    root = Path(__file__).resolve().parents[1]
    campaign = tmp_path / "campaign"
    (campaign / "brain").mkdir(parents=True)
    (campaign / "tests").mkdir()
    (campaign / "brain/__init__.py").write_text("")
    for name, value in (("first", 17), ("second", 23)):
        (campaign / f"brain/{name}.py").write_text(f"def value():\n    return {value}\n")
        (campaign / f"tests/test_{name}.py").write_text(
            f"from brain.{name} import value\ndef test_value():\n    assert value() == {value}\n")
    (campaign / "pyproject.toml").write_text('''[tool.mutmut]
source_paths = ["brain"]
pytest_add_cli_args_test_selection = ["tests/"]
process_isolation = "fork"
max_stack_depth = -1
''')
    subprocess.run(["git", "init", str(campaign)], check=True, capture_output=True)
    support = campaign / "eval/mutation_support"
    shutil.copytree(root / "eval/mutation_support", support, ignore=shutil.ignore_patterns("__pycache__"))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("BRAIN_MUTATION_") and key not in {"MUTANT_UNDER_TEST", "PYTEST_CURRENT_TEST"}}
    env.update(PYTHONPATH=f"{support}{os.pathsep}{root}", PYTEST_PLUGINS="import_check")
    for iteration, name in enumerate(("first", "second", "second")):
        if iteration == 2:
            # This synthetic contract intentionally accepts both outputs. The
            # previous kill must not survive a change in the test assertions.
            (campaign / "tests/test_second.py").write_text(
                "from brain.second import value\ndef test_value():\n    assert value() in (23, 24)\n")
        result = subprocess.run([
            sys.executable, "-c",
            "import os; from pathlib import Path; import eval.project_mutation as campaign; "
            f"campaign.ROOT=Path({str(campaign)!r}); os.chdir(campaign.ROOT); "
            f"campaign.run_shard([{{'module': 'brain/{name}.py'}}])",
        ], cwd=campaign, env=env, text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        metadata = json.loads((campaign / f"mutants/brain/{name}.py.meta").read_text())
        assert metadata["exit_code_by_key"] and set(metadata["exit_code_by_key"].values()) == ({0} if iteration == 2 else {1})
        assert "all shard candidates remain uncovered" not in result.stdout
        # Reuse the first run's cache and introduce an unrelated new test. A
        # partial incremental stats run would execute only this test and lose
        # the second module's existing behavior assertions.
        (campaign / "tests/test_extra.py").write_text("def test_unrelated():\n    assert True\n")


def test_aborted_runner_cannot_certify_cached_kills(tmp_path, monkeypatch):
    import signal
    from eval import project_mutation, mutation_types

    shadow = tmp_path / "mutants"
    shadow.mkdir()
    row = module_record(shadow, {"brain.example.x_value__mutmut_1": 1})
    proof = tmp_path / "proof.json.gz"
    proof.write_bytes(b"fixture proof; compiler validation has separate contract tests")
    output = tmp_path / "report.json"
    monkeypatch.setattr(project_mutation, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(project_mutation, "load_inventory", lambda path: {
        "modules": [row], "base": "frozen", "tool": "mutmut==3.8.0",
    })
    monkeypatch.setattr(mutation_types, "read_type_exclusions", lambda *args: {})

    def interrupted_engine(*args):
        try:
            signal.raise_signal(signal.SIGINT)
        except KeyboardInterrupt:
            pass  # The engine catches Ctrl-C during worker shutdown.

    monkeypatch.setattr(project_mutation, "run_shard", interrupted_engine)
    monkeypatch.setattr(sys, "argv", ["project_mutation", "run", "--inventory", str(tmp_path / "inventory.json"),
                                     "--type-proof", str(proof), "--output", str(output)])
    previous = signal.getsignal(signal.SIGINT)
    for expected_exit in (130, 2):
        if expected_exit == 2:
            # A successful process exit without completing verification also
            # cannot certify cached outcomes.
            def early_exit(*args):
                raise SystemExit(0)
            monkeypatch.setattr(project_mutation, "run_shard", early_exit)
        with pytest.raises(SystemExit) as stopped:
            project_mutation.main()
        assert stopped.value.code == expected_exit
        assert signal.getsignal(signal.SIGINT) == previous
        record = json.loads(output.read_text())
        assert not record["verified"] and record["measurement_exit_code"] == expected_exit
        assert record["overall"]["killed"] == 1  # Retained history is not a fresh verification.
        assert not record["overall"]["complete"] and record["overall"]["score"] is None
        assert record["modules"][0]["score"] is None
        assert record["error"]
