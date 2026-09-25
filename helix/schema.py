"""Typed DAG schema, validation and repair for Helix plans.

A plan is JSON produced by a planner model. It is only ever executed after
passing validate_plan(); the planner gets one repair pass with the exact
validation errors fed back to it.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

NODE_ID = re.compile(r"^[a-z][a-z0-9_\-]{1,40}$")

NODE_KINDS = ("research", "code", "design", "analysis", "synthesis", "verify", "custom", "agent", "exec")


class NodeSpec(BaseModel):
    id: str
    kind: str = "custom"
    task: str = Field(min_length=3)
    depends_on: list[str] = Field(default_factory=list)
    approval: bool = False
    max_tokens: int = Field(default=2048, ge=64, le=32768)
    model_tier: str = Field(default="auto", pattern="^(auto|cheap|strong)$")
    worker: str = Field(default="claude-code")  # used when kind == "agent"
    command: str = ""  # used when kind == "exec": shell command to run

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not NODE_ID.match(v):
            raise ValueError(f"node id {v!r} must match {NODE_ID.pattern}")
        return v

    @field_validator("kind")
    @classmethod
    def _kind_known(cls, v: str) -> str:
        if v not in NODE_KINDS:
            raise ValueError(f"kind must be one of {NODE_KINDS}")
        return v


class PlanSpec(BaseModel):
    goal: str = Field(min_length=3)
    nodes: list[NodeSpec] = Field(min_length=1, max_length=24)
    token_budget: int = Field(default=60000, ge=1000, le=2_000_000)


class PlanError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _topo_errors(nodes: list[NodeSpec]) -> list[str]:
    errors: list[str] = []
    ids = [n.id for n in nodes]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errors.append(f"duplicate node ids: {sorted(dupes)}")
    known = set(ids)
    for n in nodes:
        for d in n.depends_on:
            if d == n.id:
                errors.append(f"node {n.id} depends on itself")
            elif d not in known:
                errors.append(f"node {n.id} depends on unknown node {d!r}")
    indeg = {n.id: 0 for n in nodes}
    for n in nodes:
        for d in n.depends_on:
            if d in indeg:
                indeg[n.id] += 1
    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    deps = {n.id: [x for x in nodes if n.id in x.depends_on] for n in nodes}
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in deps.get(cur, []):
            indeg[nxt.id] -= 1
            if indeg[nxt.id] == 0:
                queue.append(nxt.id)
    if seen != len(nodes):
        errors.append("dependency graph contains a cycle")
    return errors


def validate_plan(data: dict[str, Any]) -> PlanSpec:
    """Parse + structurally validate a plan dict. Raises PlanError with all errors."""
    try:
        plan = PlanSpec.model_validate(data)
    except Exception as e:  # pydantic ValidationError
        raise PlanError([str(e)]) from e
    errors = _topo_errors(plan.nodes)
    if errors:
        raise PlanError(errors)
    return plan


def topo_levels(plan: PlanSpec) -> list[list[NodeSpec]]:
    """Group nodes into parallel-executable levels (Kahn layers)."""
    by_id = {n.id: n for n in plan.nodes}
    depth: dict[str, int] = {}

    def d(nid: str) -> int:
        if nid in depth:
            return depth[nid]
        node = by_id[nid]
        depth[nid] = 1 + max((d(x) for x in node.depends_on), default=-1) if node.depends_on else 0
        return depth[nid]

    for n in plan.nodes:
        d(n.id)
    levels: dict[int, list[NodeSpec]] = {}
    for nid, lv in depth.items():
        levels.setdefault(lv, []).append(by_id[nid])
    return [levels[k] for k in sorted(levels)]


def plan_metrics(plan: PlanSpec) -> dict[str, Any]:
    """Shape metrics shown in the UI / eval harness."""
    edges = sum(len(n.depends_on) for n in plan.nodes)
    levels = topo_levels(plan)
    width = max(len(lv) for lv in levels)
    return {
        "nodes": len(plan.nodes),
        "edges": edges,
        "levels": len(levels),
        "max_parallelism": width,
        "approval_gates": sum(1 for n in plan.nodes if n.approval),
    }
