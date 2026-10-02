import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from loguru import logger

from brain.config.paths import reports_dir
from brain.context.context_pack_builder import ContextPackBuilder


class GoldenTaskEvaluator:
    """Evaluation framework to run golden tasks and compute Recall/Precision metrics."""

    def __init__(self, golden_path: Optional[Path] = None):
        self.golden_path = golden_path
        self.golden_tasks: List[Dict[str, Any]] = []
        self._load_tasks()

    def _load_tasks(self):
        # Load from YAML if available
        if self.golden_path and self.golden_path.exists():
            try:
                with open(self.golden_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    self.golden_tasks = data.get(
                        "golden_tasks", data.get("holdout_tasks", data.get("independent_tasks", []))
                    )
                    logger.info(f"GoldenTaskEvaluator: Loaded {len(self.golden_tasks)} tasks from {self.golden_path}")
            except Exception as e:
                logger.error(f"Failed to load golden tasks from {self.golden_path}: {e}")

        # Fallback default tasks if none loaded
        if not self.golden_tasks:
            logger.info("GoldenTaskEvaluator: Loading default hardcoded golden tasks")
            golden_path = Path(__file__).resolve().parents[2] / "rules" / "golden_tasks.yaml"
            if golden_path.exists():
                with open(golden_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    self.golden_tasks = data.get("golden_tasks", [])
            if not self.golden_tasks:
                self.golden_tasks = [
                    {
                        "id": "task_1",
                        "description": "Update dashboard HTML branding and base template styling",
                        "expected_files": ["apps/api/templates/base.html"],
                        "expected_modules": ["apps/api/templates"],
                        "expected_features": ["branding", "dashboard"],
                        "required_surfaces": [],
                        "must_include_tests": False,
                    },
                    {
                        "id": "task_2",
                        "description": "Fix health check endpoint when database is unavailable",
                        "expected_files": ["tests/test_health.py", "apps/api/main.py"],
                        "expected_modules": ["tests", "apps/api"],
                        "expected_features": ["health check"],
                        "required_surfaces": [],
                        "must_include_tests": True,
                    },
                ]

    async def run_evaluation(self, repo_path: Path) -> Dict[str, Any]:
        """Runs all golden tasks and evaluates metrics."""
        builder = ContextPackBuilder()
        results = []

        total_precision = 0.0
        total_recall = 0.0

        # Contribution breakdown: counts of matched expected files found by each retriever
        contribution = {"lexical": 0, "vector": 0, "graph": 0, "cache": 0}

        for task in self.golden_tasks:
            desc = task["description"]
            expected_files = set(task.get("expected_files", []))
            required_surfaces = task.get("required_surfaces", [])
            must_include_tests = task.get("must_include_tests", False)

            logger.info(f"GoldenTaskEvaluator: Evaluating task: '{desc}'...")

            # Run the context builder
            res = await builder.build_context_pack(desc, repo_path, budget="standard")

            retrieved_files_list = res.get("retrieved_files", [])
            retrieved_files = {item["path"] for item in retrieved_files_list}

            # Compute intersection
            matched_files = expected_files.intersection(retrieved_files)

            # Precision@10: Fraction of retrieved files that are expected
            precision = 0.0
            if retrieved_files:
                precision = len(matched_files) / min(10, len(retrieved_files))

            # Recall@10: Fraction of expected files that are retrieved
            recall = 0.0
            if expected_files:
                recall = len(matched_files) / len(expected_files)

            total_precision += precision
            total_recall += recall

            wrong_files = list(retrieved_files - expected_files)
            missing_files = list(expected_files - retrieved_files)

            # Track retriever contributions for matched files
            for item in retrieved_files_list:
                path = item["path"]
                if path in matched_files:
                    trace = item.get("trace", {})
                    if trace.get("lexical_match"):
                        contribution["lexical"] += 1
                    if trace.get("vector_similarity", 0) > 0:
                        contribution["vector"] += 1
                    if trace.get("graph_relation"):
                        contribution["graph"] += 1
                    if trace.get("cache_match"):
                        contribution["cache"] += 1

            # Determine status
            if not expected_files:
                task_status = "FEATURE_LEVEL_ONLY"
            elif len(missing_files) == 0:
                task_status = "PASS"
            else:
                task_status = "FAIL"

            results.append(
                {
                    "id": task["id"],
                    "description": desc,
                    "expected_files": list(expected_files),
                    "expected_features": task.get("expected_features", []),
                    "retrieved_files": list(retrieved_files),
                    "matched_files": list(matched_files),
                    "precision": round(precision, 2),
                    "recall": round(recall, 2),
                    "all_expected_found": len(missing_files) == 0,
                    "wrong_files": wrong_files,
                    "missing_files": missing_files,
                    "critic_status": res.get("critic_status", "UNKNOWN"),
                    "task_status": task_status,
                    "required_surfaces": required_surfaces,
                    "must_include_tests": must_include_tests,
                }
            )

        avg_precision = total_precision / len(self.golden_tasks) if self.golden_tasks else 0.0
        avg_recall = total_recall / len(self.golden_tasks) if self.golden_tasks else 0.0

        # Write report to reports/eval-report.md
        report_output = reports_dir(repo_path)
        report_output.mkdir(parents=True, exist_ok=True)
        report_path = report_output / "eval-report.md"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        report_md = f"""# Golden Tasks Evaluation Report

**Timestamp**: {timestamp}
**Repository**: {repo_path.as_posix()}

## Summary Metrics
- **Average Precision@10**: {avg_precision:.2f}
- **Average Recall@10**: {avg_recall:.2f}

## Retriever Contribution Breakdown
- **Lexical Matches**: {contribution["lexical"]}
- **Vector Similarities**: {contribution["vector"]}
- **Graph Relationships**: {contribution["graph"]}
- **Stable Memory Cache Matches**: {contribution["cache"]}

## Detailed Task Results
"""
        for r in results:
            expected_str = ", ".join([f"`{f}`" for f in r["expected_files"]]) if r["expected_files"] else "*None*"
            retrieved_str = ", ".join([f"`{f}`" for f in r["retrieved_files"]]) if r["retrieved_files"] else "*None*"
            matched_str = ", ".join([f"`{f}`" for f in r["matched_files"]]) if r["matched_files"] else "*None*"
            wrong_str = ", ".join([f"`{f}`" for f in r["wrong_files"]]) if r["wrong_files"] else "*None*"
            missing_str = ", ".join([f"`{f}`" for f in r["missing_files"]]) if r["missing_files"] else "*None*"
            surfaces_str = ", ".join([f"`{s}`" for s in r["required_surfaces"]]) if r["required_surfaces"] else "*None*"

            report_md += f"""
### Task: {r["description"]}
- **Task ID**: {r["id"]}
- **Task Evaluation Status**: `{r["task_status"]}`
- **Critic Status**: `{r["critic_status"]}`
- **Required Surfaces**: {surfaces_str}
- **Must Include Tests**: `{r["must_include_tests"]}`
- **Expected Files**: {expected_str}
- **Retrieved Files**: {retrieved_str}
- **Matched Files**: {matched_str}
- **Missing Critical Files**: {missing_str}
- **Unrelated (Wrong) Files**: {wrong_str}
- **Metrics**: Precision@10: `{r["precision"]}`, Recall@10: `{r["recall"]}`
- **All Expected Found**: `{r["all_expected_found"]}`
---
"""
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)

        logger.info(f"GoldenTaskEvaluator: Evaluation report written to {report_path}")

        return {
            "avg_precision": round(avg_precision, 2),
            "avg_recall": round(avg_recall, 2),
            "report_path": report_path.as_posix(),
            "tasks": results,
            "contribution": contribution,
        }
