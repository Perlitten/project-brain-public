"""CLI Interface for Deep Change-Impact Analysis."""

import argparse
import json
from pathlib import Path

from brain.insights.impact_engine import ImpactAnalysisEngine


def main():
    parser = argparse.ArgumentParser(description="Project Brain Deep Change-Impact CLI")
    parser.add_argument("--repo", default=".", help="Repository root path")
    parser.add_argument("--base", default="HEAD~1", help="Base revision")
    parser.add_argument("--candidate", default="HEAD", help="Candidate revision")
    parser.add_argument("--max-depth", type=int, default=3, help="Maximum graph depth")

    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    engine = ImpactAnalysisEngine(repo_path)

    res = engine.analyze_impact(base_rev=args.base, cand_rev=args.candidate, max_depth=args.max_depth)
    print(json.dumps(res.to_dict(), indent=2))


if __name__ == "__main__":
    main()
