"""Helix SaaS API + dashboard server.

Endpoints:
  POST /api/jobs                 {goal, provider?, budget?, plan?} -> {id}
  GET  /api/jobs                 recent jobs
  GET  /api/jobs/{id}            job + plan + metrics
  GET  /api/jobs/{id}/events     event log (?since=seq)
  GET  /api/jobs/{id}/stream     SSE stream of events
  POST /api/jobs/{id}/approve    {node_id, approved, note?}
  GET  /api/stats                fleet-level totals
  POST /api/jobs/{id}/terminal   open a shell in the job's worktree -> {id, cwd, shell}
  GET  /api/terminals            live terminal sessions
  DELETE /api/terminal/{sid}     kill a session
  WS   /api/terminal/{sid}/ws    attach to a session (input/resize/output frames)
  GET  /.well-known/agent.json   A2A-style agent card
  POST /a2a                      A2A-style task submit (alias of /api/jobs)
  GET  /                         dashboard
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from pathlib import Path
from .executor import JobRunner, approve_job
from .providers import PROVIDERS
from .schema import plan_metrics, validate_plan, PlanError
from .store import Store

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "dashboard")


class JobIn(BaseModel):
    goal: str
    provider: Optional[str] = None
    budget: int = 60000
    workspace: bool = False
    plan: Optional[dict] = None


class ApprovalIn(BaseModel):
    node_id: str
    approved: bool
    note: str = ""


class TerminalIn(BaseModel):
    cols: int = 100
    rows: int = 28


def create_app(db_path: Optional[str] = None, provider: str = "mock") -> FastAPI:
    app = FastAPI(title="Helix", version=__version__)
    store = Store(db_path) if db_path else Store()
    app.state.store = store
    app.state.default_provider = provider

    def get_job_or_404(jid: str) -> dict:
        job = store.get_job(jid)
        if not job:
            raise HTTPException(404, f"no job {jid}")
        return job

    def submit_job(body: JobIn) -> dict:
        prov = body.provider or app.state.default_provider
        if prov not in PROVIDERS:
            raise HTTPException(400, f"unknown provider {prov!r}")
        jid = store.create_job(body.goal, prov)
        if body.plan is not None:
            try:
                plan = validate_plan(body.plan)
            except PlanError as e:
                raise HTTPException(422, {"errors": e.errors})
            store.update_job(jid, plan_json=json.dumps(plan.model_dump()))
        mem_root = str(Path(db_path).parent / "memory") if db_path else None
        runner = JobRunner(store, prov, token_budget=body.budget,
                           memory_root=mem_root,
                           workspace_dir="." if body.workspace else None)
        asyncio.create_task(runner.run(jid))
        return {"id": jid, "status": "queued"}

    @app.post("/api/jobs", status_code=201)
    async def submit(body: JobIn):
        return submit_job(body)

    @app.post("/a2a", status_code=201)
    async def a2a_submit(body: JobIn):
        """A2A-style task submission: same payload, peer-friendly path."""
        return submit_job(body)

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "Helix",
            "version": __version__,
            "description": "Lean DAG orchestration engine: typed plans, parallel execution, verification, approval gates, cost control.",
            "capabilities": {"streaming": True, "approvals": True, "workers": ["claude-code", "codex", "custom", "http"]},
            "endpoints": {"submit": "/a2a", "status": "/api/jobs/{id}", "events": "/api/jobs/{id}/events", "stream": "/api/jobs/{id}/stream"},
            "providers": sorted(PROVIDERS),
        }

    def playbook_store():
        from .playbooks import PlaybookStore
        root = Path(db_path).parent / "playbooks" if db_path else Path("playbooks")
        return PlaybookStore(root)

    @app.get("/api/playbooks")
    async def list_playbooks():
        return {"playbooks": playbook_store().list()}

    @app.post("/api/playbooks/{name}/run", status_code=201)
    async def run_playbook(name: str, body: JobIn):
        try:
            plan = playbook_store().get(name)
        except Exception as e:
            raise HTTPException(404, str(e))
        return submit_job(JobIn(goal=plan.goal, provider=body.provider,
                                budget=body.budget, plan=plan.model_dump()))

    @app.post("/api/hooks/{token}", status_code=201)
    async def fire_webhook(token: str):
        from .scheduler import WebhookStore
        root = Path(db_path).parent if db_path else Path(".")
        hook = WebhookStore(root).by_token(token)
        if not hook:
            raise HTTPException(404, "unknown or disabled webhook")
        body = JobIn(goal=hook.goal or f"webhook:{hook.name}",
                     provider=hook.provider, budget=hook.budget)
        if hook.playbook:
            try:
                plan = playbook_store().get(hook.playbook)
            except Exception as e:
                raise HTTPException(404, str(e))
            body.plan = plan.model_dump()
        return submit_job(body)

    @app.get("/api/jobs")
    async def list_jobs(limit: int = 50):
        return {"jobs": store.list_jobs(limit)}

    @app.get("/api/jobs/{jid}")
    async def job_detail(jid: str):
        job = get_job_or_404(jid)
        if job.get("plan"):
            try:
                job["metrics"] = plan_metrics(validate_plan(job["plan"]))
            except Exception:
                job["metrics"] = None
        return job

    @app.get("/api/jobs/{jid}/events")
    async def job_events(jid: str, since: int = 0):
        get_job_or_404(jid)
        return {"events": store.events_since(jid, seq=since)}

    @app.get("/api/jobs/{jid}/stream")
    async def job_stream(jid: str, since: int = 0):
        get_job_or_404(jid)

        async def gen():
            seq = since
            idle = 0
            while True:
                events = store.events_since(jid, seq=seq)
                for ev in events:
                    seq = ev["seq"]
                    yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                job = store.get_job(jid)
                if job and job["status"] in ("completed", "failed", "budget_exceeded") and not events:
                    yield f"event: done\ndata: {json.dumps({'status': job['status']})}\n\n"
                    return
                idle = 0 if events else idle + 1
                if idle % 20 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/jobs/{jid}/diff")
    async def job_diff(jid: str, stat: bool = False):
        job = get_job_or_404(jid)
        if not job.get("workspace_path"):
            raise HTTPException(404, "job has no workspace (submit with workspace=true)")
        from .workspaces import Workspace, WorkspaceManager
        ws = Workspace(job_id=jid, path=Path(job["workspace_path"]),
                       branch=job.get("workspace_branch") or "")
        if not ws.is_git:
            return {"diff": "", "git": False}
        return {"diff": WorkspaceManager(".").diff(ws, stat_only=stat),
                "branch": ws.branch, "git": True}

    class CommitIn(BaseModel):
        message: str = ""

    @app.post("/api/jobs/{jid}/commit")
    async def job_commit(jid: str, body: CommitIn):
        job = get_job_or_404(jid)
        if not job.get("workspace_path"):
            raise HTTPException(404, "job has no workspace")
        from .workspaces import Workspace, WorkspaceManager
        ws = Workspace(job_id=jid, path=Path(job["workspace_path"]),
                       branch=job.get("workspace_branch") or "")
        try:
            rev = WorkspaceManager(".").commit(ws, body.message or f"helix job {jid}")
        except Exception as e:
            raise HTTPException(400, str(e))
        store.emit(jid, "workspace_committed", None,
                   {"branch": ws.branch, "rev": rev})
        return {"branch": ws.branch, "rev": rev}

    @app.post("/api/jobs/{jid}/approve")
    async def approve(jid: str, body: ApprovalIn):
        get_job_or_404(jid)
        approve_job(store, jid, body.node_id, body.approved, body.note)
        return {"ok": True}

    @app.get("/api/stats")
    async def stats():
        jobs = store.list_jobs(500)
        done = [j for j in jobs if j["status"] == "completed"]
        return {
            "jobs_total": len(jobs),
            "jobs_completed": len(done),
            "tokens_total": sum(j["tokens_used"] or 0 for j in jobs),
            "cost_total_usd": round(sum(j["cost_usd"] or 0 for j in jobs), 6),
            "providers": sorted(PROVIDERS),
        }

    # ---- embedded terminals ----
    from .terminal import TerminalManager
    app.state.terminals = TerminalManager(base_cwd=os.getcwd())

    def ensure_reaper():
        task = getattr(app.state, "_reaper_task", None)
        if task is None or task.done():
            async def reap_loop():
                while True:
                    await asyncio.sleep(60)
                    app.state.terminals.reap_idle()
            app.state._reaper_task = asyncio.create_task(reap_loop())

    @app.post("/api/jobs/{jid}/terminal", status_code=201)
    async def open_terminal(jid: str, body: TerminalIn):
        job = get_job_or_404(jid)
        ensure_reaper()
        sess = app.state.terminals.create(cwd=job.get("workspace_path"),
                                          cols=body.cols, rows=body.rows)
        return {"id": sess.id, "cwd": sess.cwd, "shell": sess.shell}

    @app.get("/api/terminals")
    async def list_terminals():
        mgr = app.state.terminals
        return {"terminals": [
            {"id": s.id, "cwd": s.cwd, "shell": s.shell, "alive": s.alive,
             "clients": s.listener_count, "created_at": s.created_at}
            for s in mgr.sessions()]}

    @app.delete("/api/terminal/{sid}")
    async def kill_terminal(sid: str):
        if not app.state.terminals.kill(sid):
            raise HTTPException(404, f"no terminal {sid}")
        return {"ok": True}

    @app.websocket("/api/terminal/{sid}/ws")
    async def terminal_ws(ws: WebSocket, sid: str):
        sess = app.state.terminals.get(sid)
        if sess is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        queue, snapshot = sess.attach()
        if snapshot:
            await ws.send_json({"type": "output", "data": snapshot.decode("utf-8", "replace")})

        async def pump_out():
            try:
                while True:
                    data = await queue.get()
                    if data is None:
                        await ws.send_json({"type": "exit", "code": sess.exit_code})
                        return
                    await ws.send_json({"type": "output", "data": data.decode("utf-8", "replace")})
            except Exception:
                pass

        sender = asyncio.create_task(pump_out())
        try:
            while True:
                frame = await ws.receive_json()
                t = frame.get("type")
                if t == "input":
                    sess.write(str(frame.get("data", "")).encode("utf-8", "replace"))
                elif t == "resize":
                    sess.resize(int(frame.get("cols", 80)), int(frame.get("rows", 24)))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            sender.cancel()
            sess.detach(queue)

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(DASHBOARD_DIR, "index.html"))

    @app.get("/{path:path}")
    async def static_files(path: str):
        safe = os.path.normpath(path).lstrip("/")
        full = os.path.join(DASHBOARD_DIR, safe)
        if not full.startswith(os.path.abspath(DASHBOARD_DIR)) or not os.path.isfile(full):
            raise HTTPException(404)
        return FileResponse(full)

    return app
