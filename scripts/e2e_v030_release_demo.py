"""End-to-End Release Demonstration Scenario for v0.3.0 Release Candidate."""

import json
import shutil
import tempfile
from pathlib import Path
from brain.insights.codeowners import CodeownersParser, FindingOwnershipResolver
from brain.insights.drift_enforcement import ArchitectureChangeGuardEngine
from tests.fixtures.git_drift_fixtures import init_git_repo, run_git_cmd

def run_end_to_end_demo() -> dict:
    temp_dir = Path(tempfile.mkdtemp())
    try:
        repo_dir = temp_dir / "e2e_demo_repo"
        base_sha = init_git_repo(repo_dir)

        # 1. Clean code initial commit
        f1 = repo_dir / "clean.py"
        f1.write_text("def ok(): return 1\n", encoding="utf-8")
        run_git_cmd(repo_dir, ["add", "clean.py"])
        run_git_cmd(repo_dir, ["commit", "-m", "Clean initial commit"])

        # 2. Setup CODEOWNERS file
        codeowners_dir = repo_dir / ".github"
        codeowners_dir.mkdir(parents=True, exist_ok=True)
        (codeowners_dir / "CODEOWNERS").write_text(
            "* @platform-team\n"
            "brain/workers/ @worker-leads\n",
            encoding="utf-8"
        )

        # 3. Candidate introduces DRIFT-002 critical violation in worker
        worker_dir = repo_dir / "brain" / "workers"
        worker_dir.mkdir(parents=True, exist_ok=True)
        bad_file = worker_dir / "bad_worker.py"
        bad_file.write_text(
            "import apps.api.static\n\n"
            "def run_worker():\n"
            "    return apps.api.static.render()\n",
            encoding="utf-8"
        )
        run_git_cmd(repo_dir, ["add", "."])
        run_git_cmd(repo_dir, ["commit", "-m", "Introduce DRIFT-002 violation"])
        cand_sha = run_git_cmd(repo_dir, ["rev-parse", "HEAD"])

        # 4. Run Change Guard (Expect FAIL)
        engine = ArchitectureChangeGuardEngine(repo_dir)
        res_fail = engine.evaluate_change_guard(base_sha, cand_sha)

        # 5. Resolve Owners
        codeowners = CodeownersParser.discover_and_parse(repo_dir)
        resolver = FindingOwnershipResolver(codeowners)
        ownership = resolver.resolve_path_ownership("brain/workers/bad_worker.py")

        # 6. Apply Waiver
        brain_dir = repo_dir / ".brain"
        brain_dir.mkdir(parents=True, exist_ok=True)
        (brain_dir / "drift-waivers.yaml").write_text(
            "version: 1\n"
            "waivers:\n"
            "  - id: WAIVER-DEMO-001\n"
            "    rule_id: DRIFT-002\n"
            "    owner: worker-leads\n"
            "    reason: Temporary migration exception\n"
            "    created_at: '2026-08-01'\n"
            "    expires_at: '2099-12-31'\n",
            encoding="utf-8"
        )

        res_waived = engine.evaluate_change_guard(base_sha, cand_sha)

        # 7. Expire Waiver & Verify Status
        (brain_dir / "drift-waivers.yaml").write_text(
            "version: 1\n"
            "waivers:\n"
            "  - id: WAIVER-DEMO-001\n"
            "    rule_id: DRIFT-002\n"
            "    owner: worker-leads\n"
            "    reason: Temporary migration exception\n"
            "    created_at: '2020-01-01'\n"
            "    expires_at: '2020-01-02'\n",
            encoding="utf-8"
        )

        res_expired = engine.evaluate_change_guard(base_sha, cand_sha)

        # 8. Fix Violation
        bad_file.write_text("def run_worker(): return 42\n", encoding="utf-8")
        run_git_cmd(repo_dir, ["add", "."])
        run_git_cmd(repo_dir, ["commit", "-m", "Fix DRIFT-002 violation"])
        fix_sha = run_git_cmd(repo_dir, ["rev-parse", "HEAD"])

        res_fixed = engine.evaluate_change_guard(cand_sha, fix_sha)

        demo_summary = {
            "status": "success",
            "scenario": "v0.3.0 End-to-End Lifecycle Demonstration",
            "step_1_initial_fail": {
                "decision": res_fail.decision,
                "exit_code": res_fail.exit_code,
                "unwaived_criticals": len(res_fail.policy_reasons),
            },
            "step_2_ownership_resolved": {
                "owners": ownership.owners,
                "pattern": ownership.matching_rule.pattern if ownership.matching_rule else None,
            },
            "step_3_waived_decision": {
                "decision": res_waived.decision,
                "waived_count": res_waived.waived_count,
            },
            "step_4_expired_waiver": {
                "decision": res_expired.decision,
                "expired_waiver_count": len([e for e in res_expired.evaluated_findings if e.waiver_status == "waiver_expired"]),
            },
            "step_5_fixed_resolved": {
                "decision": res_fixed.decision,
                "exit_code": res_fixed.exit_code,
            },
        }

        return demo_summary
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    res = run_end_to_end_demo()
    print(json.dumps(res, indent=2))
