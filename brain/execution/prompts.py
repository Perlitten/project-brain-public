"""Provider-Neutral Phased Agent Execution Prompts for Project Brain v0.5.2."""

from __future__ import annotations

INVESTIGATION_PROMPT = """PHASE 1: INVESTIGATION
Goal: Inspect repository files, search code, or query advisory Brain context packs to understand the task.
DO NOT generate code edits or patch diffs during this phase.

Task Prompt: {prompt}
Allowed Tools: view_file, grep_search, list_dir, brain_context_pack

Output a brief summary of confirmed relevant files, callers, and constraints.
"""

PLANNING_PROMPT = """PHASE 2: ACTIONABLE PLAN GENERATION
Goal: Construct an explicit, structured engineering plan BEFORE any source modification.

Task Prompt: {prompt}
Confirmed Files: {confirmed_files}

You MUST output a valid JSON plan matching this exact schema:
{
  "problem_statement": "<concise description>",
  "primary_hypothesis": "<root cause hypothesis>",
  "confirmed_files": ["<path1>", "<path2>"],
  "ordered_steps": [
    {"file": "<path1>", "expected_change": "<description of edit>"}
  ],
  "required_tests": ["<test_command>"],
  "arch_constraints": [],
  "risk_level": "low" | "medium" | "high"
}
"""

IMPLEMENTATION_PROMPT = """PHASE 3: STAGED STEP IMPLEMENTATION
Goal: Execute step {step_idx} of the accepted plan.

Target File: {target_file}
Expected Change: {expected_change}

Generate a precise, bounded file replacement chunk for '{target_file}'.
"""

REPAIR_PROMPT = """PHASE 4: STRUCTURED TEST FAILURE REPAIR
Goal: Diagnose failure and propose minimal targeted correction (Attempt {attempt_idx}/{max_attempts}).

Observed Failure:
{failure_summary}

Formulate a repair hypothesis and apply a minimal correction to '{target_file}'.
"""
