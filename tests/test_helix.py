import json
import os
from pathlib import Path

import pytest

from helix.schema import PlanError, plan_metrics, topo_levels, validate_plan
from helix.eval import score_plan
from helix.providers import ModelRouter, BudgetExceeded
from helix.planner import extract_json
from helix.store import Store
from helix.executor import JobRunner, approve_job

GOOD = {"goal": "test goal", "nodes": [
    {"id": "research", "kind": "research", "task": "gather facts", "depends_on": []},
    {"id": "work_a", "kind": "analysis", "task": "workstream one", "depends_on": ["research"]},
    {"id": "work_b", "kind": "code", "task": "workstream two", "depends_on": ["research"]},
    {"id": "synthesis", "kind": "synthesis", "task": "combine it all", "depends_on": ["work_a", "work_b"]},
]}


def test_validate_good_plan():
    plan = validate_plan(GOOD)
    assert len(plan.nodes) == 4
    assert [[n.id for n in lv] for lv in topo_levels(plan)] == [["research"], ["work_a", "work_b"], ["synthesis"]]
    m = plan_metrics(plan)
    assert m["max_parallelism"] == 2 and m["edges"] == 4


def test_cycle_rejected():
    bad = {"goal": "test goal", "nodes": [
        {"id": "aa", "task": "x y", "depends_on": ["bb"]},
        {"id": "bb", "task": "y z", "depends_on": ["aa"]}]}
    with pytest.raises(PlanError):
        validate_plan(bad)


def test_unknown_dependency_rejected():
    bad = {"goal": "test goal", "nodes": [{"id": "aa", "task": "x y", "depends_on": ["ghost"]}]}
    with pytest.raises(PlanError):
        validate_plan(bad)


def test_extract_json_tolerates_prose():
    assert extract_json('here you go:\n```json\n{"a": {"b": 1}}\n```\nthanks') == {"a": {"b": 1}}
    assert extract_json('prefix {"x": "brace } inside"} suffix') == {"x": "brace } inside"}


def test_score_plan_perfect_and_degraded():
    ref = validate_plan(GOOD)
    assert score_plan(ref, ref)["exact_match"] is True
    pred = validate_plan({"goal": "test goal", "nodes": [
        {"id": "research", "kind": "research", "task": "gather facts", "depends_on": []},
        {"id": "synthesis", "kind": "synthesis", "task": "combine it all", "depends_on": ["research"]},
    ]})
    s = score_plan(pred, ref)
    assert s["node_f1"] < 1.0 and s["exact_match"] is False


def test_budget_governor():
    router = ModelRouter("mock", token_budget=100)

    async def go():
        with pytest.raises(BudgetExceeded):
            for _ in range(10):
                await router.chat([{"role": "user", "content": "write something long " * 50}])
    import asyncio
    asyncio.run(go())


def test_end_to_end_mock(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    jid = store.create_job("end to end test", "mock")
    store.update_job(jid, plan_json=json.dumps(GOOD))
    runner = JobRunner(store, "mock")

    async def go():
        return await runner.run(jid)

    import asyncio
    out = asyncio.run(go())
    assert out["status"] == "completed"
    job = store.get_job(jid)
    assert job["result"] and job["tokens_used"] > 0
    types = [e["type"] for e in store.events_since(jid)]
    assert "plan_ready" in types and "job_completed" in types


def test_approval_gate_blocks_then_passes(tmp_path):
    plan = {"goal": "gate test", "nodes": [
        {"id": "aa", "task": "do the thing", "approval": True},
    ]}
    store = Store(str(tmp_path / "t.db"))
    jid = store.create_job("gate test", "mock")
    store.update_job(jid, plan_json=json.dumps(plan))
    runner = JobRunner(store, "mock", approval_poll_s=0.1)

    async def go():
        import asyncio
        async def approver():
            seen = 0
            while True:
                for ev in store.events_since(jid, seq=seen):
                    seen = ev["seq"]
                    if ev["type"] == "approval_requested":
                        approve_job(store, jid, ev["node_id"], True, "test ok")
                        return
                await asyncio.sleep(0.05)
        t = asyncio.create_task(approver())
        out = await runner.run(jid)
        t.cancel()
        return out

    import asyncio
    out = asyncio.run(go())
    assert out["status"] == "completed"


def test_exec_sandbox(tmp_path):
    from helix.sandbox import allowed, run_exec, ExecBlocked
    assert allowed("python3 -c 'print(1)'")
    assert not allowed("rm -rf /")
    assert not allowed("cat /etc/passwd && rm x")

    async def go():
        return await run_exec("echo hello-helix")
    import asyncio
    res = asyncio.run(go())
    assert "hello-helix" in res.text

    async def blocked():
        with pytest.raises(ExecBlocked):
            await run_exec("rm -rf /tmp/x")
    asyncio.run(blocked())


def test_playbooks_roundtrip(tmp_path):
    from helix.playbooks import PlaybookStore, PlaybookError
    pbs = PlaybookStore(tmp_path / "playbooks")
    plan = validate_plan({"goal": "pb test", "nodes": [
        {"id": "first", "task": "step one"}, {"id": "second", "task": "step two", "depends_on": ["first"]}]})
    pbs.save("demo", plan)
    assert pbs.list()[0]["name"] == "demo"
    assert pbs.get("demo").goal == "pb test"
    assert pbs.delete("demo")
    try:
        pbs.get("demo")
        assert False, "should raise"
    except PlaybookError:
        pass


def test_cron_matching_and_due(tmp_path):
    from helix.scheduler import CronExpr, Schedule, ScheduleStore
    import time as _t
    # Monday 2026-09-21 09:30 local -> struct_time for matching
    tm = _t.strptime("2026-09-21 09:30", "%Y-%m-%d %H:%M")
    assert CronExpr("30 9 * * 1").matches(tm)          # Mondays at 09:30
    assert CronExpr("*/15 * * * *").matches(tm)        # every 15 min
    assert not CronExpr("31 9 * * 1").matches(tm)
    assert CronExpr("30 9 21 9 *").matches(tm)         # date-specific
    ss = ScheduleStore(tmp_path)
    ss.add(Schedule(name="morning", cron="30 9 * * *", goal="daily brief"))
    due = ss.due(now=_t.mktime(tm))
    assert [s.name for s in due] == ["morning"]
    ss.mark_run("morning", "2026-09-21 09:30")
    assert ss.due(now=_t.mktime(tm)) == []             # idempotent within the minute
    assert ss.remove("morning") and ss.list() == []


@pytest.mark.skipif(os.name == "nt", reason="sh hooks are a POSIX pattern; Windows uses .cmd/.ps1")
def test_hooks_fire_on_events(tmp_path):
    hook = tmp_path / "hooks" / "job_created"
    hook.parent.mkdir()
    out = tmp_path / "fired.json"
    hook.write_text(f"#!/bin/sh\ncat > {out}\n")
    hook.chmod(0o755)
    store = Store(str(tmp_path / "t.db"))
    jid = store.create_job("hook test", "mock")
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["type"] == "job_created" and payload["job_id"] == jid


def test_webhook_store(tmp_path):
    from helix.scheduler import Webhook, WebhookStore
    ws = WebhookStore(tmp_path)
    token = ws.add(Webhook(name="deploy", token="", playbook="ship-it"))
    assert ws.by_token(token).playbook == "ship-it"
    assert ws.by_token("wrong") is None
    assert ws.remove("deploy") and ws.list() == []


def test_worker_preset_registry_and_mcp(tmp_path):
    from helix.workers import PRESETS, available_workers, _mcp_env_and_args
    assert set(("claude-code", "codex", "gemini-cli", "aider", "goose",
                "opencode", "http", "custom")) >= set(PRESETS)
    avail = available_workers()
    assert "http" in avail and "custom" in avail
    env, args = _mcp_env_and_args(None)
    assert env == {} and args == ""
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {}}')
    env, args = _mcp_env_and_args(str(cfg))
    assert env["HELIX_MCP_CONFIG"] == str(cfg)
    assert "--mcp-config" in args


def test_worktree_job_isolation(tmp_path):
    """A worktree job edits its own branch; the base checkout stays clean."""
    import subprocess
    base = tmp_path / "proj"
    base.mkdir()
    def git(*a):
        return subprocess.run(["git", *a], cwd=base, capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    (base / "app.py").write_text("print('v1')\n")
    git("add", "-A"); git("-c", "user.email=t@t", "-c", "user.name=t",
                          "commit", "-qm", "init")
    from helix.workspaces import WorkspaceManager
    wm = WorkspaceManager(str(base))
    ws = wm.create("job_test123")
    assert ws.is_git and ws.branch == "helix/job_test123"
    (ws.path / "app.py").write_text("print('v2')\n")
    (ws.path / "new_file.py").write_text("# new\n")
    diff = wm.diff(ws)
    assert "v2" in diff and "new_file.py" in diff
    assert (base / "app.py").read_text() == "print('v1')\n"  # base untouched
    rev = wm.commit(ws, "job output")
    assert rev
    assert "nothing" not in wm.diff(ws) or wm.diff(ws).strip() == ""
    wm.remove(ws)
    assert not ws.path.exists()


def test_workspace_non_git_fallback(tmp_path):
    from helix.workspaces import WorkspaceManager
    wm = WorkspaceManager(str(tmp_path))
    ws = wm.create("job_plain")
    assert not ws.is_git and ws.path.is_dir()
    wm.remove(ws)


def test_exec_nodes_run_inside_worktree(tmp_path):
    """Regression: exec nodes must run in the job worktree, not the caller cwd."""
    import asyncio, subprocess
    base = tmp_path / "proj"
    base.mkdir()
    def git(*a):
        return subprocess.run(["git", *a], cwd=base, capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    (base / "app.py").write_text("v1\n")
    git("add", "-A"); git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    plan = {"goal": "write a file", "nodes": [
        {"id": "write_file", "kind": "exec", "task": "write out.txt",
         "command": "python3 -c \"open('out.txt','w').write('made by helix')\"",
         "depends_on": []}]}
    store = Store(str(tmp_path / "t.db"))
    jid = store.create_job("write a file", "mock")
    store.update_job(jid, plan_json=json.dumps(plan))
    runner = JobRunner(store, "mock", workspace_dir=str(base))
    out = asyncio.run(runner.run(jid))
    assert out["status"] == "completed"
    assert not (base / "out.txt").exists(), "exec leaked into the base checkout"
    ws_path = Path(store.get_job(jid)["workspace_path"])
    assert (ws_path / "out.txt").read_text() == "made by helix"


def test_list_jobs_board_fields(tmp_path):
    """The jobs board reads progress, provider and worktree info from list_jobs."""
    import json as _json
    store = Store(str(tmp_path / "t.db"))
    jid = store.create_job("board goal", "mock")
    store.update_job(jid, plan_json=_json.dumps({"goal": "g", "nodes": [
        {"id": "a", "kind": "research", "task": "t", "depends_on": []},
        {"id": "b", "kind": "code", "task": "t", "depends_on": ["a"]},
    ]}), workspace_path=str(tmp_path), workspace_branch="helix/" + jid)
    store.emit(jid, "node_completed", "a", {})
    row = [j for j in store.list_jobs(5) if j["id"] == jid][0]
    assert row["provider"] == "mock"
    assert row["nodes_total"] == 2
    assert row["nodes_done"] == 1
    assert row["workspace_branch"] == "helix/" + jid
    assert row["workspace_path"] == str(tmp_path)
    # jobs without a plan still report zeroed progress
    j2 = store.create_job("plain", "mock")
    row2 = [j for j in store.list_jobs(5) if j["id"] == j2][0]
    assert row2["nodes_total"] == 0 and row2["nodes_done"] == 0


def test_terminal_session_roundtrip(tmp_path):
    """A PTY session runs a shell, echoes input, replays scrollback, resizes, dies."""
    import time
    from helix.terminal import TerminalManager
    mgr = TerminalManager(str(tmp_path))
    sess = mgr.create()
    try:
        time.sleep(0.5)
        sess.write(b"echo helix-42\r\n")  # plain echo: runs in cmd.exe and bash alike
        deadline = time.time() + 5
        q, snap = None, b""
        while time.time() < deadline:
            q, snap = sess.attach()
            sess.detach(q)
            if b"helix-42" in snap:
                break
            time.sleep(0.2)
        assert b"helix-42" in snap
        sess.resize(132, 43)
        assert (sess.cols, sess.rows) == (132, 43)
        assert mgr.get(sess.id) is sess
    finally:
        assert mgr.kill(sess.id) is True
    assert mgr.get(sess.id) is None


def test_terminal_websocket(tmp_path):
    """The WS bridge attaches to a session and round-trips shell output."""
    import time
    from fastapi.testclient import TestClient
    from helix.api import create_app
    app = create_app(db_path=str(tmp_path / "t.db"), provider="mock")
    client = TestClient(app)
    jid = client.post("/api/jobs", json={"goal": "term test", "plan": {
        "goal": "term test",
        "nodes": [{"id": "n1", "kind": "research", "task": "noop", "depends_on": []}],
    }}).json()["id"]
    time.sleep(1.5)
    sess = client.post(f"/api/jobs/{jid}/terminal", json={}).json()
    assert sess["id"].startswith("term_") and sess["shell"]
    with client.websocket_connect(f"/api/terminal/{sess['id']}/ws") as ws:
        ws.send_json({"type": "input", "data": "echo ws-12\r\n"})  # plain echo: cmd.exe and bash alike
        out = ""
        deadline = time.time() + 5
        while time.time() < deadline and "ws-12" not in out:
            frame = ws.receive_json()
            if frame["type"] == "output":
                out += frame["data"]
        assert "ws-12" in out
    listing = client.get("/api/terminals").json()["terminals"]
    assert any(t["id"] == sess["id"] for t in listing)
    assert client.delete(f"/api/terminal/{sess['id']}").json() == {"ok": True}
