from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_distinguishes_client_labels_from_authenticated_principals():
    text = (ROOT / "docs/DASHBOARD.md").read_text(encoding="utf-8")
    for term in ("mcp:<client-name>", "mcp:unattributed", "self-reported", "authenticated principal"):
        assert term in text


def test_native_oauth_documents_deployment_and_restart_limits():
    text = (ROOT / "docs/operations/DEPLOYMENT.md").read_text(encoding="utf-8")
    for term in ("Native MCP and OAuth", "BRAIN_PUBLIC_URL", "BRAIN_PUBLIC_HOST", "HTTPS",
                 "/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server",
                 "one API worker", "sticky", "in-memory", "unverified"):
        assert term in text


def test_queue_documentation_does_not_promise_generic_cancellation_or_exactly_once():
    text = (ROOT / "docs/operations/DEPLOYMENT.md").read_text(encoding="utf-8")
    for term in ("queued-only", "JobQueue.cancel", "returns `False`", "no generic HTTP cancel endpoint",
                 "7 days", "30 days", "SQLiteJobQueue", "no cancellation method", "completed-key",
                 "not exactly-once"):
        assert term in text
