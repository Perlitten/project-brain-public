"""Tests for Project Brain n8n workflow catalog filtering."""

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import AsyncMock, call, patch

import pytest

from apps.api import helpers as helpers_module
from scripts import import_n8n_workflows


@pytest.fixture
def workflows_dir(tmp_path):
    wf_dir = tmp_path / "n8n" / "workflows"
    wf_dir.mkdir(parents=True)

    (wf_dir / "git-merge-reindex.json").write_text(
        json.dumps({"name": "Git Push → Reindex", "nodes": [{"id": "a"}]}),
        encoding="utf-8",
    )
    (wf_dir / "nightly-health.json").write_text(
        json.dumps({"name": "Nightly Harness Health", "nodes": [{"id": "b"}]}),
        encoding="utf-8",
    )
    (wf_dir / "daily-briefing.json").write_text(
        json.dumps({"name": "Daily Briefing", "nodes": []}),
        encoding="utf-8",
    )
    return wf_dir


def test_get_workflows_from_repo_only_project_brain(workflows_dir, tmp_path):
    with patch.object(helpers_module, "get_repo_root", return_value=tmp_path):
        workflows = helpers_module.get_workflows_from_repo()
    names = {wf["name"] for wf in workflows}
    assert "Git Push → Reindex" in names
    assert "Nightly Harness Health" in names
    assert "Daily Briefing" not in names
    assert all(wf["nodes_count"] > 0 for wf in workflows)


def test_import_script_covers_canonical_project_brain_workflows():
    assert set(helpers_module.PROJECT_BRAIN_WORKFLOW_FILES).issubset(set(import_n8n_workflows.WORKFLOW_FILES))


def test_deprecated_workflow_is_archived_before_delete():
    workflow = {"id": "old-workflow", "name": "Old draft", "isArchived": False}

    with patch.object(import_n8n_workflows, "_request") as request:
        import_n8n_workflows._delete_workflow("http://n8n.test", "Basic token", "session=cookie", workflow)

    assert request.call_args_list == [
        call(
            "http://n8n.test",
            "/rest/workflows/old-workflow/archive",
            auth_header="Basic token",
            cookie="session=cookie",
            data={},
            method="POST",
        ),
        call(
            "http://n8n.test",
            "/rest/workflows/old-workflow",
            auth_header="Basic token",
            cookie="session=cookie",
            method="DELETE",
        ),
    ]


def test_every_canonical_workflow_is_published_and_no_stub_is_shipped():
    assert set(import_n8n_workflows.PUBLISH_FILES) == set(import_n8n_workflows.WORKFLOW_FILES)
    workflows_dir = Path(__file__).resolve().parents[1] / "n8n" / "workflows"
    assert not (workflows_dir / "indexing-error-notify.json").exists()
    for filename in import_n8n_workflows.WORKFLOW_FILES:
        payload = json.loads((workflows_dir / filename).read_text(encoding="utf-8"))
        assert "stub" not in payload["name"].lower()
        assert all("stub" not in str(tag.get("name", "")).lower() for tag in payload.get("tags", []))
        if filename not in {
            "git-merge-reindex.json",
            "n8n-error-self-diagnosis.json",
        }:
            node_types = {node["type"] for node in payload["nodes"]}
            assert "n8n-nodes-base.scheduleTrigger" in node_types
            assert "n8n-nodes-base.executeWorkflowTrigger" in node_types
            execute_trigger = next(
                node
                for node in payload["nodes"]
                if node["type"] == "n8n-nodes-base.executeWorkflowTrigger"
            )
            assert execute_trigger["parameters"]["inputSource"] == "passthrough"


def test_publish_workflow_activates_exact_version_without_cli_restart():
    with (
        patch.object(import_n8n_workflows, "_request", return_value=({}, None)) as request,
        patch("subprocess.run") as subprocess_run,
    ):
        restart_required = import_n8n_workflows._publish_workflow(
            "http://n8n",
            "Basic token",
            "session=cookie",
            "workflow-1",
            "Canonical Workflow",
            "version-7",
        )

    assert restart_required is False
    request.assert_called_once_with(
        "http://n8n",
        "/rest/workflows/workflow-1/activate",
        auth_header="Basic token",
        cookie="session=cookie",
        data={"versionId": "version-7"},
        method="POST",
    )
    subprocess_run.assert_not_called()


def test_git_push_workflow_authenticates_before_enqueue():
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / "n8n" / "workflows" / "git-merge-reindex.json").read_text(encoding="utf-8"))
    names = [node["name"] for node in payload["nodes"]]
    assert names.index("Authorize Master Push") < names.index("Enqueue Durable Reindex")
    auth_node = next(node for node in payload["nodes"] if node["name"] == "Authorize Master Push")
    assert auth_node["type"] == "n8n-nodes-base.if"
    conditions = auth_node["parameters"]["conditions"]["conditions"]
    serialized = json.dumps(conditions)
    assert "PROJECT_BRAIN_WEBHOOK_TOKEN" in serialized
    assert "refs/heads/master" in serialized
    branches = payload["connections"]["Authorize Master Push"]["main"]
    assert branches[0][0]["node"] == "Enqueue Durable Reindex"
    assert branches[1][0]["node"] == "Reject Push"

    action = (root / ".github" / "workflows" / "project-brain-reindex.yml").read_text(encoding="utf-8")
    assert "secrets.PROJECT_BRAIN_WEBHOOK_TOKEN" in action
    assert "X-Project-Brain-Token" in action


def test_all_job_workflows_wait_for_terminal_status_and_fail_closed():
    root = Path(__file__).resolve().parents[1]
    for filename in (
        "git-merge-reindex.json",
        "nightly-health.json",
        "nightly-deep-maintenance.json",
        "nightly-proactive-insights.json",
        "weekly-benchmark.json",
    ):
        payload = json.loads((root / "n8n" / "workflows" / filename).read_text(encoding="utf-8"))
        node_types = [node["type"] for node in payload["nodes"]]
        assert "n8n-nodes-base.wait" in node_types, filename
        assert node_types.count("n8n-nodes-base.httpRequest") >= 2, filename
        assert "n8n-nodes-base.stopAndError" in node_types, filename
        serialized = json.dumps(payload)
        assert "/jobs/" in serialized, filename
        assert "failed" in serialized, filename
        assert "completed" in serialized, filename


def test_nightly_deep_workflow_is_quality_first_and_idempotent():
    root = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (
            root
            / "n8n"
            / "workflows"
            / "nightly-deep-maintenance.json"
        ).read_text(encoding="utf-8")
    )
    serialized = json.dumps(payload)

    assert "30 0 * * *" in serialized
    assert "/jobs/nightly-maintenance" in serialized
    assert "nightly-deep-maintenance:" in serialized
    assert payload["settings"]["timezone"] == "UTC"
    assert payload["settings"]["executionTimeout"] == 21_600
    assert {"quality-first", "late-interaction"}.issubset(
        {tag["name"] for tag in payload["tags"]}
    )


def test_n8n_error_workflow_enqueues_llm_self_diagnosis():
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / "n8n" / "workflows" / "n8n-error-self-diagnosis.json").read_text(encoding="utf-8"))
    node_types = {node["type"] for node in payload["nodes"]}
    assert "n8n-nodes-base.errorTrigger" in node_types
    serialized = json.dumps(payload)
    assert "/jobs/self-diagnosis" in serialized
    assert "use_llm" in serialized
    assert "trigger_summary" in serialized
    assert "Math.floor(Date.now() / 300000)" in serialized


def test_import_wires_parent_workflow_to_error_handler(tmp_path):
    workflow_path = tmp_path / "parent.json"
    workflow_path.write_text(
        json.dumps(
            {
                "name": "Parent",
                "nodes": [{"id": "a", "name": "A", "type": "test"}],
                "connections": {},
                "settings": {"timezone": "UTC"},
            }
        ),
        encoding="utf-8",
    )

    with patch.object(
        import_n8n_workflows,
        "_request",
        return_value=({"id": "created-parent"}, None),
    ) as request:
        import_n8n_workflows._import_workflow(
            "http://n8n.test",
            "Basic token",
            "session=cookie",
            workflow_path,
            {},
            error_workflow_id="error-123",
        )

    assert request.call_args.kwargs["data"]["settings"]["errorWorkflow"] == "error-123"


def test_server_up_can_append_new_secrets_to_an_existing_env():
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "server_up.sh").read_text(encoding="utf-8")
    assert 'printf \'%s=%s\\n\' "$key" "$val" >> "$ENV_FILE"' in script
    assert "ensure_secret PROJECT_BRAIN_WEBHOOK_TOKEN" in script
    assert "ensure_secret N8N_OWNER_PASSWORD" in script

    compose = (root / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert 'N8N_BLOCK_ENV_ACCESS_IN_NODE: "false"' in compose


def test_owner_setup_retries_transient_rest_router_404():
    not_ready = HTTPError(
        "http://n8n.test/rest/owner/setup",
        404,
        "Not Found",
        {},
        BytesIO(b"Cannot POST /rest/owner/setup"),
    )
    with (
        patch.object(
            import_n8n_workflows,
            "_request",
            side_effect=[not_ready, ({}, None)],
        ) as request,
        patch.object(import_n8n_workflows.time, "sleep") as sleep,
    ):
        import_n8n_workflows._setup_owner(
            "http://n8n.test",
            "Basic token",
            "brain@example.test",
            "password",
            attempts=2,
            delay_seconds=0,
        )

    assert request.call_count == 2
    sleep.assert_called_once_with(0)


def test_canonical_workflow_exports_have_stable_top_level_ids():
    workflows_dir = Path(__file__).resolve().parents[1] / "n8n" / "workflows"
    for filename in helpers_module.PROJECT_BRAIN_WORKFLOW_FILES:
        payload = json.loads((workflows_dir / filename).read_text(encoding="utf-8"))
        assert payload.get("id"), filename
        assert payload.get("name"), filename
        assert payload.get("nodes"), filename


def test_filter_project_brain_workflows_from_api():
    api_workflows = [
        {"name": "Git Push → Reindex", "source": "api", "nodes_count": 4},
        {"name": "Daily Briefing", "source": "api", "nodes_count": 0},
        {"name": "External Capture", "source": "api", "nodes_count": 0},
    ]
    with patch.object(
        helpers_module,
        "_project_brain_workflow_names",
        return_value={"Git Push → Reindex", "Nightly Harness Health"},
    ):
        filtered = helpers_module._filter_project_brain_workflows(api_workflows)
    assert len(filtered) == 1
    assert filtered[0]["name"] == "Git Push → Reindex"


@pytest.mark.asyncio
async def test_fetch_n8n_workflows_requires_api_key():
    with patch.object(helpers_module.settings, "N8N_API_KEY", None):
        workflows, error = await helpers_module.fetch_n8n_workflows_from_api()

    assert workflows == []
    assert error == "N8N_API_KEY not configured"


@pytest.mark.asyncio
async def test_get_n8n_workflows_filters_api_junk():
    api_workflows = [
        {"name": "Daily Briefing", "source": "api", "nodes_count": 0},
        {"name": "Git Push → Reindex", "source": "api", "nodes_count": 4},
    ]
    repo_workflows = [{"name": "Git Push → Reindex", "source": "repo", "nodes_count": 4}]

    with (
        patch.object(
            helpers_module,
            "fetch_n8n_workflows_from_api",
            new=AsyncMock(return_value=(api_workflows, None)),
        ),
        patch.object(
            helpers_module,
            "_project_brain_workflow_names",
            return_value={"Git Push → Reindex"},
        ),
        patch.object(
            helpers_module,
            "get_workflows_from_repo",
            return_value=repo_workflows,
        ),
    ):
        result = await helpers_module.get_n8n_workflows()

    assert result["source"] == "api"
    assert result["api_status"] == "available"
    assert result["api_workflows_count"] == 2
    assert result["repo_workflows_count"] == 1
    assert len(result["workflows"]) == 1
    assert result["workflows"][0]["name"] == "Git Push → Reindex"


@pytest.mark.asyncio
async def test_get_n8n_workflows_falls_back_to_repo_when_api_only_junk():
    api_workflows = [{"name": "Daily Briefing", "source": "api", "nodes_count": 0}]
    repo_workflows = [{"name": "Nightly Harness Health", "source": "repo", "nodes_count": 2}]

    with (
        patch.object(
            helpers_module,
            "fetch_n8n_workflows_from_api",
            new=AsyncMock(return_value=(api_workflows, None)),
        ),
        patch.object(
            helpers_module,
            "_project_brain_workflow_names",
            return_value={"Nightly Harness Health"},
        ),
        patch.object(
            helpers_module,
            "get_workflows_from_repo",
            return_value=repo_workflows,
        ),
    ):
        result = await helpers_module.get_n8n_workflows()

    assert result["source"] == "repo"
    assert result["api_status"] == "no_project_brain_workflows"
    assert result["workflows"] == repo_workflows


@pytest.mark.asyncio
async def test_get_n8n_workflows_reports_missing_api_key():
    repo_workflows = [{"name": "Nightly Harness Health", "source": "repo", "nodes_count": 2}]

    with (
        patch.object(
            helpers_module,
            "fetch_n8n_workflows_from_api",
            new=AsyncMock(return_value=([], "N8N_API_KEY not configured")),
        ),
        patch.object(
            helpers_module,
            "get_workflows_from_repo",
            return_value=repo_workflows,
        ),
    ):
        result = await helpers_module.get_n8n_workflows()

    assert result["source"] == "repo"
    assert result["api_status"] == "missing_key"
    assert result["api_error"] == "N8N_API_KEY not configured"
    assert result["repo_workflows_count"] == 1
