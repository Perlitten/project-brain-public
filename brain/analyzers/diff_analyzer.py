import asyncio
import subprocess
import re
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from loguru import logger
from brain.database.models import DiffReview
from brain.database.session import async_session_factory
from brain.llm.router import TaskKind, get_model_router
from brain.memory.rule_store import RuleStore

from brain.config.paths import get_repo_root

# Git refs come from the HTTP API. Reject anything that could be parsed as a git
# option (leading '-', e.g. --output=/--ext-diff=) or contains odd characters,
# so user input can't smuggle flags into the subprocess argv.
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9_./~^@{}:-]{1,200}$")


def _safe_git_ref(ref: str) -> bool:
    return bool(ref) and not ref.startswith("-") and bool(_GIT_REF_RE.match(ref))


class DiffAnalyzer:
    """Analyzes git diffs for rule compliance, test coverage, and code quality."""

    def __init__(self, repo_path: Optional[Path] = None):
        self.repo_path = repo_path or get_repo_root()
        self.router = get_model_router()

    async def _run_git(self, args: List[str], text: bool = False) -> subprocess.CompletedProcess:
        """Run git without blocking the asyncio event loop (required for MCP stdio)."""

        def _invoke() -> subprocess.CompletedProcess:
            command = list(args)
            if command and command[0] == "git":
                command[1:1] = [
                    "-c",
                    f"safe.directory={self.repo_path.resolve().as_posix()}",
                ]
            return subprocess.run(
                command,
                cwd=str(self.repo_path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text,
                check=True,
            )

        return await asyncio.to_thread(_invoke)

    async def _resolve_default_base(self) -> str:
        """Resolve the repository's canonical remote branch without guessing.

        ``main`` used to be hard-coded across the API, CLI and MCP adapters. That
        silently reviews the wrong lineage in repositories whose default branch
        is ``master`` (including Project Brain itself). Prefer the remote HEAD
        symbolic ref and fall back to the current commit for local-only repos.
        """
        try:
            result = await self._run_git(
                ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
                text=True,
            )
            candidate = result.stdout.strip()
            if _safe_git_ref(candidate):
                return candidate
        except Exception:
            pass
        return "HEAD"

    async def review_diff(self, base: Optional[str] = None, head: Optional[str] = None) -> dict:
        """Runs git diff, checks rules, flags missing tests and unrelated changes,

        and compiles a markdown review report.
        """
        base = base or await self._resolve_default_base()
        head = head or "current"
        logger.info(f"DiffAnalyzer: Reviewing diff between {base} and {head} in {self.repo_path}")

        if not _safe_git_ref(base) or (head != "current" and not _safe_git_ref(head)):
            return {"error": "Invalid git ref (must be a plain revision, not an option).", "status": "failed"}

        # 1. Run git diff via subprocess
        try:
            await self._run_git(["git", "rev-parse", "--is-inside-work-tree"])
        except Exception:
            return {
                "error": "Target path is not a git repository or git is not installed.",
                "status": "failed"
            }

        # Get list of modified files
        try:
            if head == "current":
                cmd_files = ["git", "diff", "--name-only", base]
            else:
                cmd_files = ["git", "diff", "--name-only", f"{base}..{head}"]

            res_files = await self._run_git(cmd_files, text=True)
            modified_files = [line.strip() for line in res_files.stdout.splitlines() if line.strip()]
        except Exception as e:
            logger.error(f"Error running git diff --name-only: {e}")
            modified_files = []

        # Get diff text
        diff_text = ""
        if modified_files:
            try:
                if head == "current":
                    cmd_diff = ["git", "diff", base]
                else:
                    cmd_diff = ["git", "diff", f"{base}..{head}"]
                res_diff = await self._run_git(cmd_diff, text=True)
                diff_text = res_diff.stdout
            except Exception as e:
                logger.error(f"Error running git diff: {e}")
                diff_text = ""

        if not modified_files:
            return {
                "status": "completed",
                "message": "No differences found between base and head.",
                "modified_files": [],
                "report_path": None,
                "markdown_content": "# Git Diff Review Report\n\nNo changes detected."
            }

        # 2. Check for missing tests (heuristic)
        test_files = [f for f in modified_files if "test" in f.lower() or "spec" in f.lower()]
        source_files = [f for f in modified_files if f not in test_files and f.endswith((".py", ".js", ".ts", ".tsx", ".kt", ".rs", ".go", ".java"))]

        missing_tests = []
        for src in source_files:
            src_stem = Path(src).stem
            # Simple heuristic: is there a test file containing the source filename stem?
            has_test = False
            for tf in test_files:
                if src_stem in tf:
                    has_test = True
                    break
            if not has_test:
                missing_tests.append(src)

        # 3. Fetch active rules
        rule_scope = self.repo_path.resolve().as_posix()
        active_rules = await RuleStore.list_active_rules(rule_scope)

        # 4. Use LLM to check rule violations, flag suspicious changes and provide general feedback
        # Truncate diff_text if it is too long to fit comfortably in context
        truncated_diff = diff_text
        if len(diff_text) > 20000:
            truncated_diff = diff_text[:20000] + "\n\n... [Diff truncated for length] ..."

        rules_str = "\n".join([f"- {r.id}: {r.name} - {r.description}" for r in active_rules])

        llm_prompt = (
            f"You are a code reviewer auditing a git diff against project rules.\n\n"
            f"Active rules:\n{rules_str}\n\n"
            f"Git diff:\n```diff\n{truncated_diff}\n```\n\n"
            f"Perform the following checks:\n"
            f"1. Identify any rule violations (specifically reference the rule ID).\n"
            f"2. Identify any suspicious or unrelated changes (e.g. debug statements like print/todo left behind, unexpected config changes, code changes out of scope).\n"
            f"3. Provide overall review feedback and code quality notes.\n\n"
            f"Respond ONLY with a JSON object containing keys:\n"
            f"- 'rule_violations': a list of objects with keys 'rule_id' and 'details' (or empty list)\n"
            f"- 'suspicious_changes': a list of strings (or empty list)\n"
            f"- 'feedback': a string containing overall review comments\n"
            f"Do not include markdown code block markers in your response."
        )

        rule_violations = []
        suspicious_changes = []
        feedback = "No feedback generated."

        try:
            response = await self.router.llm(TaskKind.SYNTHESIS).generate(
                prompt=llm_prompt,
                system_instruction="You are an expert code reviewer. Respond with valid JSON only."
            )
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
                cleaned = re.sub(r"\n```$", "", cleaned)
            review_data = json.loads(cleaned.strip())
            rule_violations = review_data.get("rule_violations", [])
            suspicious_changes = review_data.get("suspicious_changes", [])
            feedback = review_data.get("feedback", feedback)
        except Exception as e:
            logger.warning(f"Failed to run LLM diff review: {e}. Using fallback defaults.")
            # Fallback
            feedback = "Automatic LLM review unavailable. Checked files manually."

        # 5. Format Markdown Report
        violations_md = ""
        if rule_violations:
            for violation in rule_violations:
                violations_md += f"- **Rule violation**: `{violation.get('rule_id')}`: {violation.get('details')}\n"
        else:
            violations_md = "*No rule violations detected.*\n"

        test_coverage_md = ""
        if source_files:
            for src in source_files:
                if src in missing_tests:
                    test_coverage_md += f"- [ ] **{src}** (Missing corresponding test file in diff)\n"
                else:
                    test_coverage_md += f"- [x] **{src}** (Has test update/reference in diff)\n"
        else:
            test_coverage_md = "*No source files modified in this diff.*\n"

        unrelated_changes_md = ""
        if suspicious_changes:
            for item in suspicious_changes:
                unrelated_changes_md += f"- {item}\n"
        else:
            unrelated_changes_md = "*No suspicious or unrelated changes identified.*\n"

        markdown_content = f"""# Git Diff Review Report

## Summary
- **Base Branch/Commit**: `{base}`
- **Head Branch/Commit**: `{head}`
- **Files Modified**: {len(modified_files)}

## Rule Violations
{violations_md}

## Test Coverage Checklist
{test_coverage_md}

## Suspicious / Unrelated Changes
{unrelated_changes_md}

## Review Feedback / Code Quality
{feedback}
"""

        # Save review report to reports directory
        reports_dir = self.repo_path / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"diff_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        # 6. Save entry to Postgres database DiffReview
        # Get head commit hash
        try:
            commit_res = await self._run_git(["git", "rev-parse", "HEAD"], text=True)
            head_commit = commit_res.stdout.strip()
        except Exception:
            head_commit = "unknown"

        status = "approved"
        if rule_violations or missing_tests or suspicious_changes:
            status = "needs_review"

        async with async_session_factory() as session:
            review_record = DiffReview(
                commit_hash=head_commit,
                report_path=report_path.as_posix(),
                status=status
            )
            session.add(review_record)
            await session.commit()
            await session.refresh(review_record)
            review_id = review_record.id

        return {
            "id": review_id,
            "status": status,
            "modified_files": modified_files,
            "rule_violations": rule_violations,
            "missing_tests": missing_tests,
            "suspicious_changes": suspicious_changes,
            "report_path": report_path.as_posix(),
            "markdown_content": markdown_content
        }
