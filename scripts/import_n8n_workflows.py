#!/usr/bin/env python3
"""Import Project Brain n8n workflows and publish git-merge-reindex."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WORKFLOW_FILES = (
    "n8n-error-self-diagnosis.json",
    "git-merge-reindex.json",
    "nightly-deep-maintenance.json",
    "nightly-health.json",
    "nightly-proactive-insights.json",
    "weekly-benchmark.json",
)
PUBLISH_FILES = set(WORKFLOW_FILES)
DEPRECATED_WORKFLOW_NAMES = {
    "Git Merge → Reindex Pipeline",
    "Nightly Health Check (stub)",
    "Weekly Benchmark (stub)",
    "Indexing Error Notify (stub)",
    "Nightly Proactive Insights",
}


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without evaluating shell syntax."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _basic_auth_header(user: str, password: str) -> str:
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def _request(
    base: str,
    path: str,
    *,
    auth_header: str,
    cookie: str | None = None,
    data: dict[str, Any] | None = None,
    method: str = "GET",
) -> tuple[dict[str, Any], str | None]:
    headers = {"Authorization": auth_header}
    if cookie:
        headers["Cookie"] = cookie
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        return (json.loads(raw) if raw else {}), resp.headers.get("Set-Cookie")


def _wait_n8n(base: str, auth_header: str, attempts: int = 30) -> None:
    for _ in range(attempts):
        try:
            req = urllib.request.Request(f"{base.rstrip('/')}/healthz", headers={"Authorization": auth_header})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"n8n not reachable at {base}")


def _login(base: str, auth_header: str, email: str, password: str) -> str:
    payload = {"emailOrLdapLoginId": email, "password": password}
    try:
        _, cookie_hdr = _request(base, "/rest/login", auth_header=auth_header, data=payload, method="POST")
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise RuntimeError("n8n owner not configured; run owner setup first") from exc
        raise
    if not cookie_hdr:
        raise RuntimeError("n8n login did not return session cookie")
    return cookie_hdr.split(";")[0]


def _setup_owner(
    base: str,
    auth_header: str,
    email: str,
    password: str,
    *,
    attempts: int = 15,
    delay_seconds: float = 2.0,
) -> None:
    payload = {
        "email": email,
        "password": password,
        "firstName": "Brain",
        "lastName": "Admin",
    }
    for attempt in range(attempts):
        try:
            _request(
                base,
                "/rest/owner/setup",
                auth_header=auth_header,
                data=payload,
                method="POST",
            )
            print(f"Created n8n owner: {email}")
            return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="ignore")
            if exc.code in (400, 409) and ("already" in body.lower() or "exists" in body.lower()):
                return
            if exc.code == 400 and "owner" in body.lower():
                return
            # /healthz becomes ready slightly before the REST router after a
            # container recreation. Retry only that transient 404 boundary.
            if exc.code == 404 and attempt < attempts - 1:
                time.sleep(delay_seconds)
                continue
            raise RuntimeError(f"owner setup failed ({exc.code}): {body}") from exc


def _list_workflows(base: str, auth_header: str, cookie: str) -> list[dict[str, Any]]:
    payload, _ = _request(base, "/rest/workflows", auth_header=auth_header, cookie=cookie)
    return payload.get("data") or []


def _delete_workflow(base: str, auth_header: str, cookie: str, workflow: dict[str, Any]) -> None:
    wf_id = str(workflow["id"])
    name = str(workflow.get("name") or wf_id)
    if not workflow.get("isArchived"):
        _request(
            base,
            f"/rest/workflows/{wf_id}/archive",
            auth_header=auth_header,
            cookie=cookie,
            data={},
            method="POST",
        )
    _request(
        base,
        f"/rest/workflows/{wf_id}",
        auth_header=auth_header,
        cookie=cookie,
        method="DELETE",
    )
    print(f"Deleted deprecated workflow: {name} ({wf_id})")


def _import_workflow(
    base: str,
    auth_header: str,
    cookie: str,
    workflow_path: Path,
    existing: dict[str, dict[str, Any]],
    *,
    error_workflow_id: str | None = None,
) -> dict[str, Any]:
    raw = json.loads(workflow_path.read_text(encoding="utf-8"))
    name = raw["name"]
    settings = dict(raw.get("settings", {}))
    if error_workflow_id:
        settings["errorWorkflow"] = error_workflow_id
    body: dict[str, Any] = {
        "name": name,
        "nodes": raw["nodes"],
        "connections": raw["connections"],
        "settings": settings,
    }
    if raw.get("staticData") is not None:
        body["staticData"] = raw["staticData"]

    try:
        if name in existing:
            wf_id = existing[name]["id"]
            payload, _ = _request(
                base,
                f"/rest/workflows/{wf_id}",
                auth_header=auth_header,
                cookie=cookie,
                data=body,
                method="PATCH",
            )
            wf = payload.get("data") or payload
            print(f"Updated workflow: {name} ({wf_id})")
        else:
            payload, _ = _request(
                base,
                "/rest/workflows",
                auth_header=auth_header,
                cookie=cookie,
                data=body,
                method="POST",
            )
            wf = payload.get("data") or payload
            wf_id = wf["id"]
            print(f"Created workflow: {name} ({wf_id})")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"Workflow import failed for {name} ({exc.code}): {detail}") from exc

    return {
        "id": wf_id,
        "name": name,
        "file": workflow_path.name,
        "version_id": wf.get("versionId"),
    }


def _publish_workflow(
    base: str,
    auth_header: str,
    cookie: str,
    wf_id: str,
    name: str,
    version_id: str | None,
) -> bool:
    if version_id:
        try:
            _request(
                base,
                f"/rest/workflows/{wf_id}/activate",
                auth_header=auth_header,
                cookie=cookie,
                data={"versionId": version_id},
                method="POST",
            )
            print(f"Activated workflow: {name} ({version_id})")
            return False
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            print(f"  activate POST failed ({exc.code}): {detail}; falling back to CLI")
    else:
        print(f"  Workflow {name} has no versionId; falling back to CLI")

    import subprocess

    result = subprocess.run(
        ["docker", "exec", "brain-n8n", "n8n", "publish:workflow", f"--id={wf_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to publish workflow {name}: {result.stderr or result.stdout}")
    print(f"Published workflow via CLI: {name}")
    return True


def _restart_n8n(root: Path) -> None:
    import subprocess

    compose = root / "docker-compose.prod.yml"
    if not compose.exists():
        compose = root / "docker-compose.yml"
    subprocess.run(
        ["docker", "compose", "-f", str(compose), "restart", "n8n"],
        check=True,
    )


def _verify_published_cli(expected_names: set[str]) -> None:
    import subprocess

    output = "/tmp/project-brain-workflow-verification.json"
    try:
        subprocess.run(
            [
                "docker",
                "exec",
                "brain-n8n",
                "n8n",
                "export:workflow",
                "--all",
                f"--output={output}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        exported = subprocess.run(
            ["docker", "exec", "brain-n8n", "cat", output],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(exported.stdout)
    finally:
        subprocess.run(
            ["docker", "exec", "brain-n8n", "rm", "-f", output],
            check=False,
            capture_output=True,
        )

    workflows = payload if isinstance(payload, list) else [payload]
    live = {str(item.get("name")): item for item in workflows}
    inactive = [
        name
        for name in sorted(expected_names)
        if not live.get(name, {}).get("active") and not live.get(name, {}).get("activeVersionId")
    ]
    if inactive:
        raise RuntimeError(f"Imported workflows are not published: {', '.join(inactive)}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    _load_dotenv(root / ".env")
    workflows_dir = root / "n8n" / "workflows"

    base = _env("N8N_BASE_URL", _env("N8N_WEBHOOK_URL", "http://localhost:5678").rstrip("/"))
    basic_user = _env("N8N_BASIC_AUTH_USER", "admin")
    basic_pass = _env("N8N_BASIC_AUTH_PASSWORD", "changeme")
    owner_email = _env("N8N_OWNER_EMAIL", "brain-admin@local.dev")
    owner_password = _env("N8N_OWNER_PASSWORD")
    if not owner_password:
        print("ERROR: N8N_OWNER_PASSWORD must be set (no insecure default).")
        return 1

    auth_header = _basic_auth_header(basic_user, basic_pass)
    _wait_n8n(base, auth_header)
    _setup_owner(base, auth_header, owner_email, owner_password)
    cookie = _login(base, auth_header, owner_email, owner_password)

    existing = {wf["name"]: wf for wf in _list_workflows(base, auth_header, cookie)}
    imported: list[dict[str, Any]] = []
    error_workflow_id: str | None = None

    for filename in WORKFLOW_FILES:
        path = workflows_dir / filename
        if not path.exists():
            print(f"Skip missing: {filename}")
            continue
        imported_workflow = _import_workflow(
            base,
            auth_header,
            cookie,
            path,
            existing,
            error_workflow_id=(error_workflow_id if filename != "n8n-error-self-diagnosis.json" else None),
        )
        imported.append(imported_workflow)
        if filename == "n8n-error-self-diagnosis.json":
            error_workflow_id = str(imported_workflow["id"])

    for name in sorted(DEPRECATED_WORKFLOW_NAMES):
        if name not in existing:
            continue
        try:
            _delete_workflow(base, auth_header, cookie, existing[name])
        except urllib.error.HTTPError as exc:
            print(f"WARNING: deprecated draft could not be deleted: {name} ({exc.code} {exc.reason})")

    restart_required = False
    for item in imported:
        if item["file"] in PUBLISH_FILES:
            restart_required = (
                _publish_workflow(
                    base,
                    auth_header,
                    cookie,
                    item["id"],
                    item["name"],
                    item.get("version_id"),
                )
                or restart_required
            )

    if restart_required:
        _restart_n8n(root)
        _wait_n8n(base, auth_header)

    _verify_published_cli({item["name"] for item in imported})

    webhook_base = _env("N8N_WEBHOOK_URL", base).rstrip("/")
    print(f"\nImported and published {len(imported)} workflow(s).")
    print(f"Webhook: POST {webhook_base}/webhook/git-merge")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
