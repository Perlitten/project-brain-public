"""Reject mixed production imports and retain safe failed-test witnesses."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_control_failures = []


def pytest_configure(config):
    # mutmut's stats child otherwise buffers failure output into a child-only
    # CatchOutput object, hiding diagnostics from the parent process.
    if os.getenv("MUTANT_UNDER_TEST") in {"stats", "list_all_tests", "fail", ""}:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__


def pytest_collection_finish(session):
    root = Path.cwd().resolve()
    if root.name != "mutants":
        raise RuntimeError("Mutation tests must run inside the generated mutants tree")
    mixed = []
    count = 0
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] not in {"brain", "apps", "scripts"}:
            continue
        filename = getattr(module, "__file__", None)
        if filename is None:
            continue
        count += 1
        path = Path(filename).resolve()
        if not path.is_relative_to(root):
            mixed.append(f"{name}: {path}")
    if mixed:
        raise RuntimeError("Uninstrumented imports invalidate the campaign: " + "; ".join(mixed))
    print(f"Mutation import isolation: {count} project modules inside {root}")


def pytest_sessionfinish(session, exitstatus):
    target = os.getenv("BRAIN_MUTATION_STATS_CONTROL")
    phase = os.getenv("MUTANT_UNDER_TEST")
    if not target or phase not in {"stats", "", "fail"}:
        return
    from mutmut.state import state

    child_stats = os.getenv("BRAIN_MUTATION_CHILD_STATS")
    known_tests = {item.nodeid for item in session.items}
    if child_stats and phase == "stats":
        for path in sorted(Path(child_stats).glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["test"] not in known_tests:
                continue
            for function in record["functions"]:
                state().tests_by_mangled_function_name[function].add(record["test"])
    data = {"phase": phase or "clean", "exit_code": int(exitstatus), "collected": session.testscollected,
            "associated_functions": len(state().tests_by_mangled_function_name),
            "failures": _control_failures}
    Path(target).write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    mutant = os.getenv("MUTANT_UNDER_TEST", "")
    if report.failed and mutant in {"stats", "", "fail"}:
        _control_failures.append({"test": item.nodeid, "phase": report.when,
                                  "exception": call.excinfo.typename if call.excinfo else None})
    if not report.failed or "__mutmut_" not in mutant:
        return
    directory = os.getenv("BRAIN_MUTATION_WITNESSES")
    if not directory:
        return
    target = Path(directory)
    target.mkdir(exist_ok=True, parents=True)
    path = target / (hashlib.sha256(mutant.encode()).hexdigest() + ".json")
    # Omit tracebacks/local values, which can contain credentials.
    record = {"mutant": mutant, "test": item.nodeid, "phase": report.when,
              "exception": call.excinfo.typename if call.excinfo else None}
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
