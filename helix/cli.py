"""helix CLI: run, plan, jobs, status, events, watch, approve, export, eval, doctor, serve."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .executor import JobRunner, approve_job
from .planner import plan_task
from .providers import PROVIDERS, ModelRouter
from .schema import validate_plan
from .store import Store


def _store(args) -> Store:
    return Store(args.db)


def _run_job(args, store: Store, jid: str):
    """Run a job to completion, printing status; exits non-zero on failure."""
    runner = JobRunner(store, args.provider, token_budget=args.budget,
                       memory_root=None if getattr(args, "no_memory", False) else args.memory)

    async def auto_approve():
        if not getattr(args, "auto_approve", False):
            return
        seen = 0
        while True:
            for ev in store.events_since(jid, seq=seen):
                seen = ev["seq"]
                if ev["type"] == "approval_requested":
                    approve_job(store, jid, ev["node_id"], True, "auto-approved by CLI")
            await asyncio.sleep(0.5)

    async def main():
        t = asyncio.create_task(auto_approve())
        out = await runner.run(jid)
        t.cancel()
        return out

    out = asyncio.run(main())
    job = store.get_job(jid)
    print(f"status: {out['status']} | tokens: {job['tokens_used']} | cost: ${job['cost_usd']}")
    if out["status"] == "completed":
        print("\n===== RESULT =====\n")
        print(job["result"])
    else:
        print(f"error: {out.get('error')}")
        sys.exit(1)


def cmd_run(args):
    store = _store(args)
    jid = store.create_job(args.goal, args.provider)
    print(f"job {jid} created - running on provider {args.provider}")
    _run_job(args, store, jid)


def _playbooks(args):
    from .playbooks import PlaybookStore
    return PlaybookStore(Path(args.db).parent / "playbooks")


def cmd_playbook(args):
    pbs = _playbooks(args)
    if args.action == "list":
        for pb in pbs.list():
            if "error" in pb:
                print(f"{pb['name']:<24} ERROR: {pb['error']}")
            else:
                print(f"{pb['name']:<24} {pb['nodes']} nodes  {pb['goal'][:50]}")
        return
    if args.action == "show":
        print(pbs.get(args.name).model_dump_json(indent=2))
        return
    if args.action == "delete":
        print(f"deleted {args.name}" if pbs.delete(args.name) else f"no playbook {args.name}")
        return
    if args.action == "save":
        if args.template:
            import importlib.resources as ir
            plan = validate_plan(json.loads(
                ir.files("helix.templates").joinpath(f"{args.template}.json").read_text()))
        elif args.file:
            plan = validate_plan(json.load(open(args.file)))
        else:
            if not args.goal:
                sys.exit("playbook save needs --goal or --file")
            router = ModelRouter(args.provider)
            plan = asyncio.run(plan_task(args.goal, router))
        pbs.save(args.name, plan)
        print(f"saved playbook {args.name} ({len(plan.nodes)} nodes, goal: {plan.goal[:50]})")
        return
    if args.action == "run":
        plan = pbs.get(args.name)
        store = _store(args)
        jid = store.create_job(plan.goal, args.provider)
        store.update_job(jid, plan_json=plan.model_dump_json())
        print(f"job {jid} created from playbook {args.name} (no re-planning)")
        _run_job(args, store, jid)
        return
    sys.exit(f"unknown playbook action {args.action}")


def cmd_plan(args):
    router = ModelRouter(args.provider)
    mem_ctx = ""
    if not args.no_memory:
        from .memory import MemoryStore
        mem_ctx = MemoryStore(Path(args.memory)).recall_text(args.goal)

    async def main():
        plan = await plan_task(args.goal, router, memory_context=mem_ctx)
        print(json.dumps(plan.model_dump(), indent=2))

    asyncio.run(main())


def cmd_jobs(args):
    for j in _store(args).list_jobs(args.limit):
        print(f"{j['id']}  {j['status']:<18} ${j['cost_usd']:<8} {j['goal'][:60]}")


def cmd_status(args):
    job = _store(args).get_job(args.job_id)
    if not job:
        sys.exit(f"no job {args.job_id}")
    print(json.dumps({k: v for k, v in job.items() if k != "result"}, indent=2, default=str))
    if job.get("result") and args.full:
        print("\n===== RESULT =====\n")
        print(job["result"])


def cmd_events(args):
    for ev in _store(args).events_since(args.job_id, seq=args.since):
        data = json.dumps(ev["data"]) if ev.get("data") is not None else ""
        print(f"{ev['seq']:>5}  {ev['type']:<20} {ev['node_id'] or '-':<15} {data[:100]}")


def cmd_watch(args):
    """Live terminal view of a running job: event tail + status, refreshed."""
    store = _store(args)
    seen = 0
    try:
        while True:
            for ev in store.events_since(args.job_id, seq=seen):
                seen = ev["seq"]
                data = ""
                if ev.get("data"):
                    d = ev["data"]
                    data = d.get("reason") or d.get("model") or d.get("note") or ""
                print(f"  {ev['type']:<20} {ev['node_id'] or '-':<13} {str(data)[:80]}")
            job = store.get_job(args.job_id)
            if not job:
                sys.exit(f"no job {args.job_id}")
            if job["status"] in ("completed", "failed", "budget_exceeded"):
                print(f"\n{args.job_id}: {job['status']} | tokens {job['tokens_used']} | ${job['cost_usd']}")
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n(watch stopped; the job keeps running)")


def cmd_approve(args):
    store = _store(args)
    approve_job(store, args.job_id, args.node, not args.reject, args.note)
    print(("rejected" if args.reject else "approved") + f" {args.node} on {args.job_id}")


def cmd_export(args):
    store = _store(args)
    job = store.get_job(args.job_id)
    if not job:
        sys.exit(f"no job {args.job_id}")
    bundle = {"job": job, "events": store.events_since(args.job_id)}
    with open(args.out, "w") as f:
        json.dump(bundle, f, indent=2, default=str)
    print(f"exported {args.job_id} -> {args.out}")


def cmd_eval(args):
    """Score the planner against a gold set of reference decompositions."""
    from .eval import score_plan
    from .schema import validate_plan
    gold = json.load(open(args.gold))
    router = ModelRouter(args.provider)

    async def one(g):
        pred = await plan_task(g["goal"], router)
        return score_plan(pred, validate_plan(g["plan"]))

    async def main():
        rows = []
        for g in gold:
            s = await one(g)
            rows.append(s)
            print(f"{g['goal'][:52]:<54} node_f1={s['node_f1']:<7} edge_f1={s['edge_f1']:<7} po={s['partial_order_accuracy']:<7} exact={s['exact_match']}")
        n = len(rows)
        avg = {k: round(sum(r[k] for r in rows) / n, 4) for k in ("node_f1", "edge_f1", "partial_order_accuracy")}
        exact = sum(1 for r in rows if r["exact_match"])
        print(f"\nMEAN over {n} goals: {avg} | exact match: {exact}/{n}")
        print(f"provider={args.provider} (scores are reproducible with the same provider + gold set)")

    asyncio.run(main())


def _schedules(args):
    from .scheduler import ScheduleStore
    return ScheduleStore(Path(args.db).parent)


def cmd_templates(args):
    import importlib.resources as ir
    for f in sorted(ir.files("helix.templates").iterdir()):
        if f.name.endswith(".json"):
            plan = validate_plan(json.loads(f.read_text()))
            print(f"{f.name[:-5]:<22} {len(plan.nodes)} nodes  {plan.goal[:52]}")


def cmd_schedule(args):
    from .scheduler import Schedule
    ss = _schedules(args)
    if args.action == "list":
        for s in ss.list():
            state = "on " if s.enabled else "off"
            what = f"playbook:{s.playbook}" if s.playbook else s.goal[:40]
            print(f"{s.name:<20} [{state}] {s.cron:<18} {what}")
        return
    if args.action == "add":
        if not args.name:
            sys.exit("schedule add needs a name")
        ss.add(Schedule(name=args.name, cron=args.cron, goal=args.goal or "",
                        playbook=args.playbook or "", provider=args.provider,
                        budget=args.budget))
        print(f"scheduled {args.name}: {args.cron}")
        return
    if args.action == "remove":
        print(f"removed {args.name}" if ss.remove(args.name) else f"no schedule {args.name}")
        return
    if args.action == "daemon":
        print(f"helix scheduler daemon - checking every {args.interval}s (Ctrl-C to stop)", flush=True)
        store = _store(args)
        try:
            while True:
                for s in ss.due():
                    ss.mark_run(s.name, time.strftime("%Y-%m-%d %H:%M"))
                    goal = s.goal
                    jid = store.create_job(goal or f"playbook:{s.playbook}", s.provider)
                    if s.playbook:
                        plan = _playbooks(args).get(s.playbook)
                        store.update_job(jid, plan_json=plan.model_dump_json())
                    print(f"[{time.strftime('%H:%M')}] fired {s.name} -> job {jid}", flush=True)
                    run_args = argparse.Namespace(
                        provider=s.provider, budget=s.budget,
                        memory=args.memory, no_memory=False,
                        auto_approve=args.auto_approve)
                    try:
                        _run_job(run_args, store, jid)
                    except SystemExit:
                        pass  # failed job is already recorded in the store
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n(scheduler stopped)")
        return
    sys.exit(f"unknown schedule action {args.action}")


def cmd_webhook(args):
    from .scheduler import Webhook, WebhookStore
    ws = WebhookStore(Path(args.db).parent)
    if args.action == "list":
        for h in ws.list():
            what = f"playbook:{h.playbook}" if h.playbook else h.goal[:36]
            print(f"{h.name:<18} POST /api/hooks/{h.token}  ->  {what}")
        return
    if args.action == "add":
        if not args.name:
            sys.exit("webhook add needs a name")
        token = ws.add(Webhook(name=args.name, token="", goal=args.goal or "",
                               playbook=args.playbook or "", provider=args.provider,
                               budget=args.budget))
        print(f"webhook {args.name}: POST /api/hooks/{token}")
        return
    if args.action == "remove":
        print(f"removed {args.name}" if ws.remove(args.name) else f"no webhook {args.name}")
        return
    sys.exit(f"unknown webhook action {args.action}")


def _mcp_file(args) -> Path:
    return Path(args.db).parent / "mcp.json"


def cmd_mcp(args):
    """Manage the MCP server config passed through to workers."""
    f = _mcp_file(args)
    cfg = json.loads(f.read_text()) if f.exists() else {"mcpServers": {}}
    if args.action == "list":
        for name, srv in cfg.get("mcpServers", {}).items():
            print(f"{name:<20} {srv.get('command', '')} {' '.join(srv.get('args', []))}")
        if not cfg.get("mcpServers"):
            print(f"(no MCP servers configured; file: {f})")
        return
    if args.action == "add-server":
        if not args.name or not args.command:
            sys.exit("mcp add-server needs a name and --command")
        import shlex
        cfg.setdefault("mcpServers", {})[args.name] = {
            "command": args.command, "args": shlex.split(args.args or "")}
        f.write_text(json.dumps(cfg, indent=2))
        print(f"added MCP server {args.name} -> {f} (workers get it via passthrough)")
        return
    if args.action == "remove":
        if cfg.get("mcpServers", {}).pop(args.name, None) is None:
            sys.exit(f"no MCP server {args.name}")
        f.write_text(json.dumps(cfg, indent=2))
        print(f"removed MCP server {args.name}")
        return
    sys.exit(f"unknown mcp action {args.action}")


def cmd_memory(args):
    """List recalled learnings, or append one with --add."""
    from .memory import MemoryStore
    ms = MemoryStore(Path(args.memory))
    if args.add:
        ms.remember(args.add, source="cli")
        print("remembered.")
        return
    if args.recall:
        print(ms.recall_text(args.recall) or "(no relevant memories)")
        return
    lf = ms.learnings_file
    print(lf.read_text() if lf.exists() else "(memory empty)")


def cmd_doctor(args):
    """Environment self-check: providers with keys, workers on PATH, DB writable."""
    import os
    import shutil
    from .workers import available_workers
    print("providers:")
    for name, cfg in PROVIDERS.items():
        if name == "mock":
            state = "ready (offline)"
        elif not cfg["key_env"]:
            state = "ready (no key needed)"
        else:
            state = "key set" if os.environ.get(cfg["key_env"]) else f"missing {cfg['key_env']}"
        print(f"  {name:<12} {state}")
    print("workers:")
    for name, ok in available_workers().items():
        print(f"  {name:<12} {'available' if ok else 'not found'}")
    try:
        s = _store(args)
        s.list_jobs(1)
        print(f"database: writable ({args.db})")
    except Exception as e:
        print(f"database: ERROR {e}")


def cmd_serve(args):
    import uvicorn
    from .api import create_app
    app = create_app(db_path=args.db, provider=args.provider)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def main(argv=None):
    p = argparse.ArgumentParser(prog="helix", description="Helix - lean DAG orchestration engine")
    p.add_argument("--db", default=None, help="SQLite path (default ~/.helix/helix.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--provider", default="mock", choices=list(PROVIDERS))
        sp.add_argument("--memory", default=None,
                        help="memory dir (default: <db dir>/memory)")
        sp.add_argument("--no-memory", action="store_true",
                        help="disable memory recall for this run")

    sp = sub.add_parser("run", help="plan and execute a goal end to end")
    sp.add_argument("goal")
    sp.add_argument("--budget", type=int, default=60000)
    sp.add_argument("--auto-approve", action="store_true", help="auto-approve human gates")
    common(sp)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("plan", help="print the validated DAG for a goal, no execution")
    sp.add_argument("goal")
    common(sp)
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("jobs", help="list recent jobs")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_jobs)

    sp = sub.add_parser("status", help="show one job")
    sp.add_argument("job_id")
    sp.add_argument("--full", action="store_true", help="include the result text")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("events", help="show the event log for a job")
    sp.add_argument("job_id")
    sp.add_argument("--since", type=int, default=0)
    sp.set_defaults(fn=cmd_events)

    sp = sub.add_parser("watch", help="live terminal view of a running job")
    sp.add_argument("job_id")
    sp.add_argument("--interval", type=float, default=1.5)
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("approve", help="approve or reject a pending gate")
    sp.add_argument("job_id")
    sp.add_argument("node")
    sp.add_argument("--reject", action="store_true")
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_approve)

    sp = sub.add_parser("export", help="export a job + full event log as JSON")
    sp.add_argument("job_id")
    sp.add_argument("out")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("eval", help="score the planner against reference DAGs")
    sp.add_argument("--gold", default="examples/gold_plans.json")
    common(sp)
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("mcp", help="manage MCP servers passed through to workers")
    sp.add_argument("action", choices=["add-server", "list", "remove"])
    sp.add_argument("name", nargs="?", default=None)
    sp.add_argument("--command", default=None)
    sp.add_argument("--args", default=None, help="server args as one quoted string")
    sp.set_defaults(fn=cmd_mcp)

    sp = sub.add_parser("webhook", help="inbound webhooks: POST fires a goal/playbook")
    sp.add_argument("action", choices=["add", "list", "remove"])
    sp.add_argument("name", nargs="?", default=None)
    sp.add_argument("--goal", default=None)
    sp.add_argument("--playbook", default=None)
    sp.add_argument("--budget", type=int, default=60000)
    sp.add_argument("--provider", default="mock", choices=list(PROVIDERS))
    sp.set_defaults(fn=cmd_webhook)

    sp = sub.add_parser("templates", help="list built-in plan templates")
    sp.set_defaults(fn=cmd_templates)

    sp = sub.add_parser("schedule", help="cron schedules for goals or playbooks")
    sp.add_argument("action", choices=["add", "list", "remove", "daemon"])
    sp.add_argument("name", nargs="?", default=None)
    sp.add_argument("--cron", default="0 9 * * *", help="5-field cron expr")
    sp.add_argument("--goal", default=None)
    sp.add_argument("--playbook", default=None)
    sp.add_argument("--budget", type=int, default=60000)
    sp.add_argument("--interval", type=float, default=30, help="daemon poll seconds")
    sp.add_argument("--auto-approve", action="store_true")
    sp.add_argument("--provider", default="mock", choices=list(PROVIDERS))
    sp.add_argument("--memory", default=None)
    sp.set_defaults(fn=cmd_schedule)

    sp = sub.add_parser("playbook", help="save/run named plans")
    sp.add_argument("action", choices=["save", "run", "list", "show", "delete"])
    sp.add_argument("name", nargs="?", default=None)
    sp.add_argument("--goal", default=None, help="plan this goal and save it")
    sp.add_argument("--file", default=None, help="save a plan JSON file instead of planning")
    sp.add_argument("--template", default=None, help="save a built-in template (see: helix templates)")
    sp.add_argument("--budget", type=int, default=60000)
    sp.add_argument("--auto-approve", action="store_true")
    sp.add_argument("--provider", default="mock", choices=list(PROVIDERS))
    sp.add_argument("--memory", default=None)
    sp.add_argument("--no-memory", action="store_true")
    sp.set_defaults(fn=cmd_playbook)

    sp = sub.add_parser("memory", help="show learnings, or --add / --recall")
    sp.add_argument("--add", default=None, help="append a learning")
    sp.add_argument("--recall", default=None, help="recall memories for a query")
    sp.set_defaults(fn=cmd_memory)

    sp = sub.add_parser("doctor", help="environment self-check")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("serve", help="run the API + dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8741)
    common(sp)
    sp.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    if args.db is None:
        from .store import DEFAULT_DB
        args.db = DEFAULT_DB
    if getattr(args, "memory", None) is None:
        args.memory = str(Path(args.db).parent / "memory")
    args.fn(args)


if __name__ == "__main__":
    main()
