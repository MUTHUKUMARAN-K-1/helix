"""Planner: task -> validated DAG. One repair pass on validation failure."""
from __future__ import annotations

import json
import re

from .providers import ModelRouter
from .schema import PlanError, PlanSpec, validate_plan

PLAN_PROMPT = """You are the Helix planner. Turn the goal below into an execution plan as JSON.

Rules:
- JSON only, no prose, no markdown fences.
- Shape: {"goal": str, "token_budget": int, "nodes": [Node]}
- Node: {"id": str, "kind": one of research|code|design|analysis|synthesis|verify|custom|agent|exec,
  "task": str, "depends_on": [id], "approval": bool, "max_tokens": int,
  "model_tier": one of auto|cheap|strong}
- ids: lowercase, 2-40 chars, [a-z0-9_-], must start with a letter.
- 3 to 12 nodes. Independent nodes must NOT depend on each other (they run in parallel).
- End with exactly one synthesis node that depends on all leaf workstreams.
- Mark approval=true on nodes whose output a human should review before downstream work.
- Use model_tier "strong" only for synthesis or hard code nodes; everything else "cheap".

GOAL:
{goal}

PLAN_JSON:"""

REPAIR_PROMPT = """The plan JSON below failed validation. Fix ONLY the listed errors and return corrected JSON.

ERRORS:
{errors}

BROKEN PLAN:
{broken}

FIXED PLAN_JSON:"""


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of model output, tolerating fences/prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    start = text.find("{")
    if start == -1:
        raise PlanError(["planner returned no JSON object"])
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as e:
        raise PlanError([f"planner returned invalid JSON: {e}"])
    if not isinstance(obj, dict):
        raise PlanError(["planner JSON is not an object"])
    return obj


async def plan_task(goal: str, router: ModelRouter, memory_context: str = "") -> PlanSpec:
    """Plan with one repair pass; raises PlanError if still invalid."""
    mem = ("\n\nRELEVANT MEMORY FROM PRIOR JOBS (apply these learnings):\n"
           + memory_context) if memory_context else ""
    messages = [
        {"role": "system", "content": "You output only valid JSON execution plans."},
        {"role": "user", "content": PLAN_PROMPT.replace("{goal}", goal) + mem},
    ]
    first = await router.chat(messages, tier="cheap", max_tokens=4096, json_mode=True)
    try:
        plan = validate_plan(extract_json(first.text))
        plan.goal = goal
        return plan
    except PlanError as e:
        repair = await router.chat([
            {"role": "system", "content": "You output only valid JSON execution plans."},
            {"role": "user", "content": REPAIR_PROMPT.replace("{errors}", "\n".join(f"- {x}" for x in e.errors)).replace("{broken}", first.text)},
        ], tier="cheap", max_tokens=4096, json_mode=True)
        plan = validate_plan(extract_json(repair.text))
        plan.goal = goal
        return plan
