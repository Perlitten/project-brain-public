#!/usr/bin/env python3
"""Dry-run classifier for active decision memory; never deletes history."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

from brain.database.models import Decision
from brain.database.session import async_session_factory


def classify(title: str, description: str | None = None) -> str:
    text = f"{title} {description or ''}".casefold()
    if any(token in text for token in ("security", "architecture", "adr", "invariant", "must", "policy")):
        return "normative"
    # Explicit audit/checkpoint records stay distinguishable from ordinary
    # operational activity even when their description mentions a deployment.
    if any(token in text for token in ("audit", "checkpoint")):
        return "audit_checkpoint"
    if any(token in text for token in ("release", "deploy", "reindex", "backfill", "migration run")):
        return "operational"
    if any(token in text for token in ("verified", "complete", "completed")):
        return "audit_checkpoint"
    return "historical_review"


async def _run(apply: bool) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        decisions = list((await session.execute(select(Decision).order_by(Decision.id))).scalars().all())
        rows = []
        for decision in decisions:
            classification = classify(decision.title, decision.description)
            proposed_status = "historical" if classification in {"operational", "audit_checkpoint", "historical_review"} else decision.status
            row = {
                "id": decision.id,
                "title": decision.title,
                "current_status": decision.status,
                "classification": classification,
                "proposed_status": proposed_status,
                "would_change": proposed_status != decision.status,
            }
            rows.append(row)
            if apply and row["would_change"]:
                decision.status = proposed_status
        if apply:
            await session.commit()
        return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Mark proposed rows historical; never deletes records")
    args = parser.parse_args()
    rows = asyncio.run(_run(args.apply))
    artifact = {
        "mode": "apply" if args.apply else "dry_run",
        "count": len(rows),
        "counts": dict(Counter(row["classification"] for row in rows)),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{artifact['mode']}: {artifact['count']} decisions -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
