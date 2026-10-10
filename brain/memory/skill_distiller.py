"""L3 → L4: distill repeated procedural learnings into skills.

A skill is a reusable workflow, not a fact. The distiller looks for groups of
active, repo-scoped learnings in procedural categories (workflow, testing,
repo) that no skill has consumed yet, asks the summarizer for a named
procedure, and stores it as a ``pending`` skill (or ``active`` when
``MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL`` is off). Pending skills are
never matched or injected until POST /skills/{id}/approve.

Runs at the end of every consolidation pass (best-effort) and is also
callable on its own.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from brain.config.settings import settings
from brain.database.models import Learning, MemorySkill
from brain.database.session import async_session_factory
from brain.llm import get_summarizer_provider
from brain.memory.repo_scope import normalize_repo_scope

logger = logging.getLogger(__name__)

# Only procedural categories can become a procedure; infra/llm-behavior facts stay L3.
PROCEDURAL_CATEGORIES = ("workflow", "testing", "repo")
# Hard cap per run so one noisy repo cannot starve the LLM budget.
MAX_SKILLS_PER_RUN = 5

_DISTILL_SKILL_PROMPT = """These durable learnings from one repository describe how work gets done there.
Turn them into ONE reusable procedure an AI coding agent should follow.

Learnings:
{learnings}

Reply with JSON only:
{{"name": "short imperative name (<= 80 chars)",
  "description": "when and why to apply it (1-2 sentences)",
  "triggers": ["2-5 short phrases a task description would contain"],
  "workflow": [{{"step": 1, "action": "..."}}, ...],
  "confidence": 0.0-1.0,
  "procedural": true|false}}
Set "procedural": false if the learnings are isolated facts rather than a repeatable workflow."""


async def _eligible_groups(min_learnings: int) -> Dict[str, List[Learning]]:
    """repo_scope → active procedural learnings not yet referenced by any skill."""
    async with async_session_factory() as session:
        used: set[int] = set()
        for ids in (await session.execute(select(MemorySkill.source_learning_ids))).scalars().all():
            for lid in ids or []:
                try:
                    used.add(int(lid))
                except (TypeError, ValueError):
                    continue
        rows = (
            await session.execute(
                select(Learning).where(
                    Learning.status == "active",
                    Learning.repo_scope.isnot(None),
                    Learning.category.in_(PROCEDURAL_CATEGORIES),
                ).order_by(Learning.repo_scope, Learning.id)
            )
        ).scalars().all()
    groups: Dict[str, List[Learning]] = defaultdict(list)
    for row in rows:
        if row.id in used:
            continue
        scope = normalize_repo_scope(row.repo_scope)
        if scope:
            groups[scope].append(row)
    return {scope: items for scope, items in groups.items() if len(items) >= min_learnings}


def _parse_skill_json(raw: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("procedural", True):
        return None
    name = str(data.get("name", "")).strip()[:80]
    description = str(data.get("description", "")).strip()
    if not name or not description:
        return None
    triggers = [str(t).strip()[:64] for t in (data.get("triggers") or []) if str(t).strip()][:8]
    workflow_raw = data.get("workflow") or []
    workflow: List[Dict[str, Any]] = []
    for i, step in enumerate(workflow_raw[:12], start=1):
        if isinstance(step, dict):
            action = str(step.get("action") or step.get("step") or "").strip()
        else:
            action = str(step).strip()
        if action:
            workflow.append({"step": i, "action": action[:400]})
    if not workflow:
        return None
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return {"name": name, "description": description, "triggers": triggers,
            "workflow": workflow, "confidence": confidence}


async def distill_skills(*, min_learnings: Optional[int] = None) -> Dict[str, Any]:
    """One L3 → L4 pass. Returns {"status", "groups", "created": [...], "skipped": [...]}."""
    from brain.memory.skill_store import SkillConflictError, create_skill

    needed = min_learnings or max(2, settings.MEMORY_SKILL_DISTILLATION_MIN_LEARNINGS)
    groups = await _eligible_groups(needed)
    report: Dict[str, Any] = {"status": "ok", "groups": len(groups), "created": [], "skipped": []}
    if not groups:
        return report
    summarizer = get_summarizer_provider()
    status = "pending" if settings.MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL else "active"
    for scope, learnings in sorted(groups.items())[:MAX_SKILLS_PER_RUN]:
        text = "\n".join(f"- [{lrng.id}] ({lrng.category}) {lrng.statement}" for lrng in learnings[:20])
        try:
            raw = await summarizer.summarize(_DISTILL_SKILL_PROMPT.format(learnings=text))
        except Exception as exc:
            report["skipped"].append({"repo_scope": scope, "reason": f"summarizer: {type(exc).__name__}: {exc}"})
            continue
        parsed = _parse_skill_json(raw or "")
        if parsed is None:
            report["skipped"].append({"repo_scope": scope, "reason": "not procedural or unparsable"})
            continue
        name = parsed["name"]
        try:
            skill = await create_skill(
                name=name,
                description=parsed["description"],
                triggers=parsed["triggers"],
                workflow=parsed["workflow"],
                source_episode_ids=[],
                source_learning_ids=[lrng.id for lrng in learnings[:20]],
                confidence=parsed["confidence"],
                repo_scope=scope,
                is_global=False,
                status=status,
            )
        except SkillConflictError:
            # Same name already exists: the procedure is known. Do not retry
            # the same group forever — fold the new learnings into the existing skill.
            merged = await _attach_learnings_to_existing(name, [lrng.id for lrng in learnings[:20]])
            report["skipped"].append({"repo_scope": scope, "reason": f"skill '{name}' exists", "merged": merged})
            continue
        except Exception as exc:
            report["skipped"].append({"repo_scope": scope, "reason": f"create_skill: {type(exc).__name__}: {exc}"})
            continue
        report["created"].append({"skill_id": skill.id, "name": skill.name, "status": skill.status,
                                  "repo_scope": scope, "source_learning_ids": skill.source_learning_ids})
    logger.info("Skill distillation: %d groups, %d created, %d skipped",
                report["groups"], len(report["created"]), len(report["skipped"]))
    return report


async def _attach_learnings_to_existing(name: str, learning_ids: List[int]) -> bool:
    async with async_session_factory() as session:
        skill = (await session.execute(select(MemorySkill).where(MemorySkill.name == name))).scalar_one_or_none()
        if skill is None:
            return False
        known = [int(x) for x in (skill.source_learning_ids or [])]
        skill.source_learning_ids = known + [lid for lid in learning_ids if lid not in known]
        await session.commit()
        return True
