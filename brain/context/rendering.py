"""Context-pack presentation, independent of selection, storage and providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from brain.database.models import File, Symbol, Rule, Decision, Learning


@dataclass(frozen=True)
class RenderedEvidence:
    files: str
    symbols: str


@dataclass(frozen=True)
class PackDocument:
    task_description: str
    task_type: str
    features: list[str]
    budget: str
    evidence: RenderedEvidence
    graph_md: str
    rules: list[Rule]
    decisions: list[Decision]
    learnings: list[Learning]
    risks: list[str]
    checklist: list[str]
    plan_content: str
    critic_status: str
    critic_warnings: list[str]


def render_evidence(
    final_files: list[tuple[File, dict[str, Any]]],
    final_symbols: list[tuple[Symbol, dict[str, Any]]],
    repo_posix: str,
) -> RenderedEvidence:
    """Render traces before graph I/O, retaining legacy formatting and errors."""
    files_md = ""
    if final_files:
        for f, t in final_files:
            trace_str = (
                f"RRF Score: {t.get('final_score', 0)} "
                f"[Lexical: {t.get('lexical_match', False)}, Vector: {t.get('vector_similarity', 0):.2f}, "
                f"Graph: {t.get('graph_relation', False)}, Cache: {t.get('cache_match', False)}]"
            )
            files_md += f"- [{f.path}](file:///{repo_posix}/{f.path}) - {f.summary or 'No summary'}\n  * *Trace*: {trace_str}\n"
    else:
        files_md = "*No files found matching the task keywords.*\n"

    symbols_md = ""
    if final_symbols:
        for s, t in final_symbols:
            trace_str = (
                f"RRF Score: {t['final_score']} "
                f"[Lexical: {t['lexical_match']}, Graph: {t['graph_relation']}, Cache: {t['cache_match']}]"
            )
            symbols_md += f"- `{s.name}` ({s.kind}) - {s.summary or 'No summary'}\n  * *Trace*: {trace_str}\n"
    else:
        symbols_md = "*No symbols found matching the task keywords.*\n"

    return RenderedEvidence(files_md, symbols_md)


def render_document(document: PackDocument) -> str:
    """Preserve the complete artifact contract, including fallbacks and whitespace."""
    task_description = document.task_description
    task_type = document.task_type
    features = document.features
    budget = document.budget
    graph_md = document.graph_md
    active_rules = document.rules
    active_decisions = document.decisions
    active_learnings = document.learnings
    risks = document.risks
    checklist = document.checklist
    plan_content = document.plan_content
    files_md = document.evidence.files
    symbols_md = document.evidence.symbols
    rules_md = ""
    if active_rules:
        for r in active_rules:
            rules_md += f"- **{r.id}** ({r.severity or 'medium'} severity): {r.description or 'No description'}\n"
    else:
        rules_md = "*No active rules defined.*\n"

    decisions_md = ""
    if active_decisions:
        for d in active_decisions:
            decisions_md += f"- **{d.title}** ({d.status}): {d.description or 'No description'}\n"
    else:
        decisions_md = "*No active decisions recorded.*\n"

    learnings_md = ""
    if active_learnings:
        for lrng in active_learnings[:10]:
            learnings_md += f"- {lrng.statement} _(confidence {lrng.confidence:.2f})_\n"
    else:
        learnings_md = "*No consolidated learnings yet.*\n"

    risks_md = ""
    if risks:
        for risk in risks:
            risks_md += f"- {risk}\n"
    else:
        risks_md = "*No specific risks identified.*\n"

    checklist_md = ""
    if checklist:
        for item in checklist:
            checklist_md += f"- [ ] {item}\n"
    else:
        checklist_md = "- [ ] Verify that implementation changes are correct.\n"

    # Format warnings/critic status
    critic_status = document.critic_status
    critic_warnings_md = ""
    if document.critic_warnings:
        critic_warnings_md = "### Critic Warnings\n"
        for w in document.critic_warnings:
            critic_warnings_md += f"> [!WARNING]\n> {w}\n\n"

    title_snippet = re.sub(r"[^a-zA-Z0-9\s]", "", task_description[:50]).strip()
    markdown_content = f"""# Context Pack: {title_snippet}

## Critic Evaluation Status: **{critic_status}**
{critic_warnings_md}
## Task Details
- **Task Type**: {task_type}
- **Description**: {task_description}
- **Features**: {", ".join(features) if features else "None"}
- **Token Budget Mode**: {budget}

## Relevant Features & Files
### Files
{files_md}

### Symbols
{symbols_md}

## Graph Neighbors (Context)
{graph_md}

## Active Rules & Decisions
### Rules
{rules_md}

### Decisions
{decisions_md}

### Consolidated Learnings
{learnings_md}

## Known Risks
{risks_md}

## Suggested Implementation Plan
{plan_content}

## Verification Checklist
{checklist_md}
"""

    return markdown_content
