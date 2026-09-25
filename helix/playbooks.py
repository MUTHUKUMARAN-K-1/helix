"""Named, reusable plans.

A playbook is a validated PlanSpec stored as JSON under
``<db dir>/playbooks/<name>.json``. Save one from a fresh planner run or
from a plan file you trust, then run it by name - no re-planning cost,
same DAG every time. Files are portable: commit them, share them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .schema import PlanSpec, validate_plan

_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class PlaybookError(ValueError):
    pass


def _check_name(name: str):
    if not _NAME.match(name):
        raise PlaybookError(
            f"invalid playbook name {name!r}: letters, digits, - and _ only")


class PlaybookStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        _check_name(name)
        return self.root / f"{name}.json"

    def save(self, name: str, plan: PlanSpec) -> Path:
        path = self._path(name)
        path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        return path

    def get(self, name: str) -> PlanSpec:
        path = self._path(name)
        if not path.exists():
            raise PlaybookError(f"no playbook {name!r} in {self.root}")
        return validate_plan(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                plan = validate_plan(json.loads(p.read_text(encoding="utf-8")))
                out.append({"name": p.stem, "goal": plan.goal,
                            "nodes": len(plan.nodes)})
            except Exception as e:
                out.append({"name": p.stem, "error": str(e)})
        return out

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False
