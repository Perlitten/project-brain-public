"""v0.3.0 API and CLI Contract Smoke Suite."""

import json
from pathlib import Path
from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app


def run_contract_smoke() -> dict:
    repo_root = Path(__file__).resolve().parent.parent

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        endpoints_to_test = [
            ("GET", "/insights/architectural-drift/summary"),
            ("GET", "/insights/architectural-drift/findings"),
            ("GET", "/insights/architectural-drift/trends"),
            ("GET", "/insights/architectural-drift/policy"),
            ("GET", "/insights/architectural-drift/waivers"),
            ("GET", "/insights/architectural-drift/reviews"),
            ("POST", "/insights/architectural-drift/check?base_rev=HEAD~1&candidate_rev=HEAD"),
            ("POST", "/insights/architectural-drift/pr-review?base_rev=HEAD~1&candidate_rev=HEAD"),
        ]

        results = {}
        for method, endpoint in endpoints_to_test:
            url = f"{endpoint}{'&' if '?' in endpoint else '?'}repo_path={repo_root}"
            if method == "GET":
                resp = client.get(url)
            else:
                resp = client.post(url)

            results[endpoint] = {
                "status_code": resp.status_code,
                "passed": resp.status_code in {200, 404},
            }

        return {
            "status": "success",
            "total_endpoints_tested": len(results),
            "all_passed": all(v["passed"] for v in results.values()),
            "details": results,
        }
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    res = run_contract_smoke()
    print(json.dumps(res, indent=2))
