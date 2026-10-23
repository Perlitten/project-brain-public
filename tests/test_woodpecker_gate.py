"""A missing, partial or stale CI result must never authorize a release."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import woodpecker_gate as gate


SHA = "a" * 40


def pipeline(**changes):
    return {
        "number": 7, "commit": SHA, "event": "push", "branch": "main",
        "ref": "refs/heads/main", "status": "success",
        "workflows": [{"name": name, "state": "success"} for name in gate.REQUIRED_WORKFLOWS],
        **changes,
    }


def test_complete_main_pipeline_passes():
    gate.verified_pipeline(pipeline(), SHA)


@pytest.mark.parametrize("name", sorted(gate.REQUIRED_WORKFLOWS))
def test_every_release_workflow_runs_on_unrelated_main_pushes(name):
    """A README-only merge still needs a complete release receipt for its own SHA."""
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / ".woodpecker" / f"{name}.yaml").read_text(encoding="utf-8"))
    eligible = []
    for condition in config["when"]:
        events = condition.get("event", [])
        events = [events] if isinstance(events, str) else events
        branches = condition.get("branch", [])
        branches = [branches] if isinstance(branches, str) else branches
        if "push" in events and (not branches or "main" in branches):
            eligible.append(condition)
    assert any("path" not in condition and "evaluate" not in condition for condition in eligible), name


@pytest.mark.parametrize("changes", [
    {"commit": "b" * 40}, {"event": "pull_request"}, {"event": "manual"},
    {"branch": "topic"}, {"ref": "refs/tags/v0.9.2"}, {"status": "running"},
    {"status": "failure"}, {"status": "skipped"}, {"workflows": []},
])
def test_partial_or_other_revision_cannot_authorize_release(changes):
    with pytest.raises(ValueError):
        gate.verified_pipeline(pipeline(**changes), SHA)


@pytest.mark.parametrize("state", ["skipped", "pending", "running", "failure", "killed", None])
def test_green_pipeline_with_incomplete_workflow_is_rejected(state):
    result = deepcopy(pipeline())
    result["workflows"][0]["state"] = state
    with pytest.raises(ValueError):
        gate.verified_pipeline(result, SHA)


def test_pagination_and_newest_red_run(monkeypatch):
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            import json
            return json.dumps(self.payload).encode()

    def fetch(request, timeout):
        requests.append(request.full_url)
        if "page=1" in request.full_url:
            return Response([pipeline(number=1)] + [pipeline(commit="b" * 40)] * 49)
        if "page=2" in request.full_url:
            return Response([pipeline(number=8, status="failure")])
        assert request.full_url.endswith("/pipelines/8")
        return Response(pipeline(number=8, status="failure"))

    monkeypatch.setattr(gate.urllib.request, "urlopen", fetch)
    with pytest.raises(ValueError, match="not green"):
        gate.verify_revision(SHA, "https://ci.example.invalid", "4", "synthetic")
    assert len(requests) == 3


@pytest.mark.parametrize("server", ["http://ci.example.invalid", "https://u:p@ci.example.invalid",
                                    "https://ci.example.invalid/path", "https://ci.example.invalid?token=x"])
def test_invalid_server_is_rejected_before_credentials_are_sent(server):
    with pytest.raises(ValueError):
        gate.verify_revision(SHA, server, "4", "synthetic")
