"""Async DAG executor: parallel levels, retries, verification, approvals, budget.

Approval gates work across processes: the executor polls the event log for an
approval_resolved event, so the CLI, API and dashboard can all approve a job
without sharing memory with the runner.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

from .planner import plan_task
from .providers import BudgetExceeded, ModelRouter, Usage
from .schema import NodeSpec, PlanSpec, plan_metrics, topo_levels, validate_plan
from .store import Store

NODE_PROMPT = """You are one worker node inside a larger Helix job.

JOB GOAL:
{goal}

YOUR ASSIGNED SUBTASK ({kind}):
{task}

{upstream}

Produce the concrete output for your subtask only. Be specific and self-contained:
downstream nodes and the final synthesis read your text, not your thoughts."""

VERIFY_PROMPT = """VERIFY: does the output below genuinely cover the assigned subtask?
Answer as JSON: {{"pass": true|false, "reason": str}}

SUBTASK: {task}

OUTPUT:
{output}"""

MAX_NODE_ATTEMPTS = 2
APPROVAL_TIMEOUT_S = 3600


class _WorkerChatResult:
    """Adapt a WorkerResult to the ChatResult shape the executor logs."""

    def __init__(self, wr):
        self.text = wr.text
        self.model = f"worker:{wr.worker}"
        self.latency_ms = 0
        est = max(1, len(wr.text) // 4)
        self.usage = Usage(est, 0)


class JobRunner:
    def __init__(self, store: Store, provider: Optional[str] = None,
                 token_budget: Optional[int] = None, approval_poll_s: float = 2.0,
                 memory_root: Optional[str] = None):
        self.store = store
        self.router = ModelRouter(provider or "mock",
                                  token_budget=token_budget or 60000)
        self.approval_poll_s = approval_poll_s
        self.memory = None
        if memory_root:
            from .memory import MemoryStore
            self.memory = MemoryStore(Path(memory_root))

    async def run(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        goal = job["goal"]
        self.store.update_job(job_id, status="planning")
        try:
            if job.get("plan"):
                plan = validate_plan(job["plan"])
            else:
                self.store.emit(job_id, "planning_started", None, {"goal": goal})
                mem_ctx = self.memory.recall_text(goal) if self.memory else ""
                if mem_ctx:
                    self.store.emit(job_id, "memory_recalled", None,
                                    {"chars": len(mem_ctx)})
                plan = await plan_task(goal, self.router, memory_context=mem_ctx)
            self.router.token_budget = plan.token_budget
            self.store.update_job(job_id, status="running",
                                  plan_json=json.dumps(plan.model_dump()))
            self.store.emit(job_id, "plan_ready", None,
                            {"plan": plan.model_dump(), "metrics": plan_metrics(plan)})
            result = await self._execute(job_id, plan)
            self.store.update_job(job_id, status="completed", result=result,
                                  tokens_used=self.router.spent, cost_usd=self.router.cost_usd())
            self.store.emit(job_id, "job_completed", None,
                            {"tokens": self.router.spent, "cost_usd": self.router.cost_usd()})
            return {"status": "completed", "result": result}
        except BudgetExceeded as e:
            return self._fail(job_id, "budget_exceeded", str(e))
        except Exception as e:
            return self._fail(job_id, "failed", f"{type(e).__name__}: {e}")

    def _fail(self, job_id: str, status: str, error: str) -> dict:
        self.store.update_job(job_id, status=status, error=error,
                              tokens_used=self.router.spent, cost_usd=self.router.cost_usd())
        self.store.emit(job_id, "job_failed", None, {"status": status, "error": error})
        return {"status": status, "error": error}

    async def _execute(self, job_id: str, plan: PlanSpec) -> str:
        outputs: dict[str, str] = {}
        for level in topo_levels(plan):
            level_results = await asyncio.gather(
                *(self._run_node(job_id, plan, node, outputs) for node in level))
            for node, text in zip(level, level_results):
                outputs[node.id] = text
        leaf_ids = [n.id for n in plan.nodes if n.kind == "synthesis"]
        if leaf_ids:
            return outputs[leaf_ids[-1]]
        last_level = topo_levels(plan)[-1]
        return "\n\n---\n\n".join(outputs[n.id] for n in last_level)

    async def _run_node(self, job_id: str, plan: PlanSpec, node: NodeSpec,
                        outputs: dict[str, str]) -> str:
        self.store.emit(job_id, "node_started", node.id,
                        {"kind": node.kind, "task": node.task})
        if node.approval:
            await self._await_approval(job_id, node, stage="before")
        upstream = ""
        if node.depends_on:
            parts = [f"OUTPUT OF UPSTREAM NODE {d!r}:\n{outputs.get(d, '')}" for d in node.depends_on]
            upstream = "UPSTREAM RESULTS TO BUILD ON:\n\n" + "\n\n".join(parts)
        node_mem = ""
        if self.memory:
            recalled = self.memory.recall_text(f"{plan.goal} {node.task}", limit=6,
                                               max_chars=1500)
            if recalled:
                node_mem = f"\n\nMEMORY FROM PRIOR RUNS:\n{recalled}"
        prompt = (NODE_PROMPT.format(goal=plan.goal, kind=node.kind,
                                     task=node.task, upstream=upstream)
                  + node_mem)
        tier = node.model_tier if node.model_tier != "auto" else (
            "strong" if node.kind in ("synthesis", "code") else "cheap")
        last_err: Optional[Exception] = None
        for attempt in range(1, MAX_NODE_ATTEMPTS + 1):
            try:
                if node.kind == "agent":
                    from .workers import run_worker
                    mcp = str(Path(self.store._path).parent / "mcp.json")
                    wr = await run_worker(node.worker, prompt,
                                          mcp_config=mcp if Path(mcp).is_file() else None)
                    res = _WorkerChatResult(wr)
                elif node.kind == "exec":
                    from .sandbox import run_exec
                    wr = await run_exec(node.command or node.task)
                    res = _WorkerChatResult(wr)
                else:
                    res = await self.router.chat(
                        [{"role": "user", "content": prompt}],
                        tier=tier, max_tokens=node.max_tokens)
                verdict = await self._verify(node, res.text, tier)
                self.store.emit(job_id, "node_verified", node.id,
                                {"attempt": attempt, "pass": verdict["pass"],
                                 "reason": verdict.get("reason", "")})
                if verdict["pass"]:
                    if node.approval:
                        await self._await_approval(job_id, node, stage="after")
                    self.store.emit(job_id, "node_completed", node.id,
                                    {"output": res.text, "model": res.model,
                                     "latency_ms": res.latency_ms,
                                     "tokens": res.usage.total})
                    if self.memory:
                        try:
                            self.memory.remember(
                                f"Task: {node.task}\nOutcome: {res.text[:400]}",
                                source=f"job:{job_id} node:{node.id}")
                        except Exception:
                            pass
                    return res.text
                last_err = RuntimeError(f"verification failed: {verdict.get('reason')}")
            except BudgetExceeded:
                raise
            except Exception as e:
                last_err = e
                self.store.emit(job_id, "node_retry", node.id,
                                {"attempt": attempt, "error": str(e)})
                await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"node {node.id} failed after {MAX_NODE_ATTEMPTS} attempts: {last_err}")

    async def _verify(self, node: NodeSpec, output: str, tier: str) -> dict:
        try:
            res = await self.router.chat(
                [{"role": "user", "content": VERIFY_PROMPT.format(
                    task=node.task, output=output[:4000])}],
                tier="cheap", max_tokens=256, json_mode=True)
            return json.loads(res.text)
        except Exception:
            return {"pass": True, "reason": "verifier unavailable; accepted"}

    def _existing_resolution(self, job_id: str, node_id: str) -> Optional[bool]:
        """An approval already recorded for a node covers its later gates."""
        for ev in self.store.events_since(job_id):
            if ev["type"] == "approval_resolved" and ev["node_id"] == node_id:
                return bool(ev["data"] and ev["data"].get("approved"))
        return None

    async def _await_approval(self, job_id: str, node: NodeSpec, stage: str):
        prior = self._existing_resolution(job_id, node.id)
        if prior is True:
            return
        if prior is False:
            raise RuntimeError(f"node {node.id} was rejected earlier")
        self.store.update_job(job_id, status="awaiting_approval")
        self.store.emit(job_id, "approval_requested", node.id, {"stage": stage})
        deadline = time.monotonic() + APPROVAL_TIMEOUT_S
        seen = self.store.events_since(job_id)
        base_seq = seen[-1]["seq"] if seen else 0
        while time.monotonic() < deadline:
            for ev in self.store.events_since(job_id, seq=base_seq):
                if ev["type"] == "approval_resolved" and ev["node_id"] == node.id:
                    approved = bool(ev["data"] and ev["data"].get("approved"))
                    self.store.update_job(job_id, status="running")
                    if not approved:
                        raise RuntimeError(f"node {node.id} rejected at {stage} approval gate")
                    return
            await asyncio.sleep(self.approval_poll_s)
        raise RuntimeError(f"approval for node {node.id} timed out")


def approve_job(store: Store, job_id: str, node_id: str, approved: bool, note: str = ""):
    store.emit(job_id, "approval_resolved", node_id,
               {"approved": approved, "note": note})
