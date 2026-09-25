/* Helix dashboard: live DAG canvas, SSE event stream, approvals, results. */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { jobId: null, plan: null, nodeState: {}, es: null, seq: 0, pendingApproval: null, pollTimer: null };

  const PALETTES = {
    light: {
      queued:  { stroke: "rgba(15,23,42,0.25)", fill: "#ffffff", text: "#64748b" },
      running: { stroke: "#0284c7", fill: "rgba(2,132,199,0.07)", text: "#0284c7" },
      waiting: { stroke: "#d97706", fill: "rgba(217,119,6,0.07)", text: "#d97706" },
      verified:{ stroke: "#0d9488", fill: "rgba(13,148,136,0.07)", text: "#0d9488" },
      done:    { stroke: "#059669", fill: "rgba(5,150,105,0.08)", text: "#059669" },
      failed:  { stroke: "#dc2626", fill: "rgba(220,38,38,0.06)", text: "#dc2626" },
      edge: "rgba(15,23,42,0.18)", edgeActive: "rgba(5,150,105,0.6)", nodeText: "#1e293b",
    },
    dark: {
      queued:  { stroke: "rgba(255,255,255,0.22)", fill: "rgba(255,255,255,0.04)", text: "#8b95a7" },
      running: { stroke: "#38bdf8", fill: "rgba(56,189,248,0.10)", text: "#38bdf8" },
      waiting: { stroke: "#fbbf24", fill: "rgba(251,191,36,0.10)", text: "#fbbf24" },
      verified:{ stroke: "#5eead4", fill: "rgba(94,234,212,0.10)", text: "#5eead4" },
      done:    { stroke: "#34d399", fill: "rgba(52,211,153,0.12)", text: "#34d399" },
      failed:  { stroke: "#f87171", fill: "rgba(248,113,113,0.10)", text: "#f87171" },
      edge: "rgba(255,255,255,0.13)", edgeActive: "rgba(52,211,153,0.55)", nodeText: "#dbe3ee",
    },
  };
  const theme = () => document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  const COLORS = new Proxy({}, { get: (_, k) => PALETTES[theme()][k] });
  const NODE_W = 176, NODE_H = 58, PAD_X = 90, PAD_Y = 46;

  async function api(path, opts) {
    const r = await fetch(path, opts && { method: opts.method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.body || {}) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  async function refreshStats() {
    try {
      const s = await api("/api/stats");
      $("st-jobs").textContent = s.jobs_total;
      $("st-done").textContent = s.jobs_completed;
      $("st-tokens").textContent = fmtNum(s.tokens_total);
      $("st-cost").textContent = "$" + s.cost_total_usd.toFixed(2);
    } catch (e) {}
  }

  async function refreshJobs() {
    try {
      const { jobs } = await api("/api/jobs?limit=30");
      const box = $("jobs");
      box.innerHTML = "";
      for (const j of jobs) {
        const el = document.createElement("div");
        el.className = "job-card" + (j.id === state.jobId ? " active" : "");
        el.innerHTML = `<div class="goal"></div>
          <div class="meta"><span class="pill ${j.status}">${j.status.replace("_", " ")}</span>
          <span>${fmtNum(j.tokens_used || 0)} tok</span><span>$${(j.cost_usd || 0).toFixed(2)}</span></div>`;
        el.querySelector(".goal").textContent = j.goal;
        el.onclick = () => selectJob(j.id);
        box.appendChild(el);
      }
    } catch (e) {}
  }

  function fmtNum(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  async function selectJob(jid) {
    if (state.es) state.es.close();
    state.jobId = jid; state.seq = 0; state.nodeState = {}; state.plan = null; state.pendingApproval = null;
    $("events").innerHTML = "";
    $("result").innerHTML = '<span class="placeholder">The final synthesis lands here when the job completes.</span>';
    $("approval-banner").classList.remove("show");
    const job = await api(`/api/jobs/${jid}`);
    $("stage-title").textContent = job.goal;
    $("stage-jobid").textContent = job.id;
    setStatusPill(job.status);
    if (job.plan) { state.plan = job.plan; for (const n of job.plan.nodes) state.nodeState[n.id] = "queued"; }
    if (job.result) showResult(job.result);
    $("empty-stage").style.display = "none";
    draw();
    refreshJobs();
    const { events } = await api(`/api/jobs/${jid}/events?since=0`);
    for (const ev of events) { handleEvent(ev, true); state.seq = Math.max(state.seq, ev.seq); }
    $("events").scrollTop = $("events").scrollHeight;
    if (!["completed", "failed", "budget_exceeded"].includes(job.status)) openStream(jid);
    refreshApprovals(events);
    refreshChanges();
  }

  function openStream(jid) {
    $("live-dot").classList.remove("off");
    const es = new EventSource(`/api/jobs/${jid}/stream?since=${state.seq}`);
    state.es = es;
    es.onmessage = (m) => { const ev = JSON.parse(m.data); handleEvent(ev); state.seq = Math.max(state.seq, ev.seq); };
    es.addEventListener("done", () => { es.close(); $("live-dot").classList.add("off"); finalRefresh(); });
    es.onerror = () => { es.close(); $("live-dot").classList.add("off"); pollFallback(); };
  }

  function pollFallback() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      if (!state.jobId) return;
      const { events } = await api(`/api/jobs/${state.jobId}/events?since=${state.seq}`);
      for (const ev of events) { handleEvent(ev); state.seq = Math.max(state.seq, ev.seq); }
      const job = await api(`/api/jobs/${state.jobId}`);
      setStatusPill(job.status);
      if (job.result) showResult(job.result);
      refreshApprovals(events);
      if (!["completed", "failed", "budget_exceeded"].includes(job.status)) pollFallback();
      else finalRefresh();
    }, 1500);
  }

  function finalRefresh() {
    refreshJobs(); refreshStats(); refreshChanges();
    if (state.jobId) api(`/api/jobs/${state.jobId}`).then(j => { if (j.result) showResult(j.result); setStatusPill(j.status); });
  }

  function handleEvent(ev, replay = false) {
    const t = ev.type, n = ev.node_id;
    if (t === "plan_ready") {
      state.plan = ev.data.plan;
      for (const nd of ev.data.plan.nodes) state.nodeState[nd.id] = "queued";
      setStatusPill("running");
    }
    if (t === "node_started" || t === "node_retry") state.nodeState[n] = "running";
    if (t === "node_verified" && ev.data.pass) state.nodeState[n] = "verified";
    if (t === "node_completed") state.nodeState[n] = "done";
    if (t === "approval_requested") { state.nodeState[n] = "waiting"; if (!replay) showApproval(ev); }
    if (t === "approval_resolved") { if (state.nodeState[n] === "waiting") state.nodeState[n] = "running"; hideApproval(); }
    if (t === "job_completed") { setStatusPill("completed"); finalRefresh(); }
    if (t === "job_failed") { setStatusPill(ev.data && ev.data.status || "failed"); finalRefresh(); }
    appendEvent(ev, replay);
    draw();
  }

  function appendEvent(ev, replay = false) {
    const box = $("events");
    const el = document.createElement("div");
    el.className = "ev";
    if (replay) el.style.opacity = "1";
    const detail = ev.data && ev.data.reason ? ev.data.reason
      : ev.data && ev.data.model ? `${ev.data.model} · ${ev.data.tokens} tok · ${ev.data.latency_ms}ms`
      : ev.data && ev.data.note ? ev.data.note : "";
    el.innerHTML = `<span class="seq">${ev.seq}</span><span class="type t-${ev.type}">${ev.type}</span><span class="node">${ev.node_id || ""}</span><span class="detail"></span>`;
    el.querySelector(".detail").textContent = detail;
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }

  function refreshApprovals(events) {
    const pending = {};
    for (const ev of events || []) {
      if (ev.type === "approval_requested") pending[ev.node_id + ev.seq] = ev;
      if (ev.type === "approval_resolved") {
        for (const k of Object.keys(pending)) if (pending[k].node_id === ev.node_id) delete pending[k];
      }
    }
    const left = Object.values(pending);
    if (left.length) showApproval(left[left.length - 1]); else hideApproval();
  }

  function showApproval(ev) {
    state.pendingApproval = ev;
    $("approval-text").innerHTML = `<b>${ev.node_id}</b> is holding at a human gate (${ev.data && ev.data.stage || "review"}). Approve to continue the job.`;
    $("approval-banner").classList.add("show");
  }
  function hideApproval() { state.pendingApproval = null; $("approval-banner").classList.remove("show"); }

  async function resolveApproval(approved) {
    if (!state.pendingApproval) return;
    await api(`/api/jobs/${state.jobId}/approve`, { body: { node_id: state.pendingApproval.node_id, approved, note: approved ? "approved from dashboard" : "rejected from dashboard" } });
    hideApproval();
  }
  $("btn-approve").onclick = () => resolveApproval(true);
  $("btn-reject").onclick = () => resolveApproval(false);

  function setStatusPill(status) {
    const p = $("stage-status");
    p.style.display = "";
    p.className = "pill " + status;
    p.textContent = status.replace("_", " ");
  }

  function showResult(text) { const r = $("result"); r.textContent = text; r.classList.add("result-view"); }

  function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  function renderDiff(text) {
    if (!text || !text.trim()) return '<span class="placeholder">Workspace clean - no changes.</span>';
    return text.split("\n").map(l => {
      let cls = "dl-ctx";
      if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff --git")) cls = "dl-file";
      else if (l.startsWith("@@")) cls = "dl-hunk";
      else if (l.startsWith("+")) cls = "dl-add";
      else if (l.startsWith("-")) cls = "dl-del";
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join("");
  }

  async function refreshChanges() {
    if (!state.jobId) return;
    try {
      const d = await api(`/api/jobs/${state.jobId}/diff`);
      state.hasWorkspace = true;
      $("changes").innerHTML = renderDiff(d.diff);
      $("btn-commit").style.display = (d.diff && d.diff.trim()) ? "" : "none";
    } catch (e) {
      state.hasWorkspace = false;
      $("changes").innerHTML = '<span class="placeholder">This job has no worktree. Launch with "Isolate in a git worktree".</span>';
      $("btn-commit").style.display = "none";
    }
  }

  function showTab(name) {
    for (const t of ["result", "changes", "terminal"]) {
      $("tab-" + t).classList.toggle("active", t === name);
      $(t).style.display = t === name ? "" : "none";
    }
    const isTerm = name === "terminal";
    $("term-cwd").style.display = isTerm ? "" : "none";
    $("btn-term-restart").style.display = isTerm ? "" : "none";
    $("btn-commit").style.display = "none";
    if (name === "changes") refreshChanges();
    if (isTerm) openTerminal();
  }
  $("tab-result").onclick = () => showTab("result");
  $("tab-changes").onclick = () => showTab("changes");
  $("tab-terminal").onclick = () => showTab("terminal");
  $("btn-commit").onclick = async () => {
    $("btn-commit").disabled = true;
    try {
      const r = await api(`/api/jobs/${state.jobId}/commit`, { body: {} });
      $("changes").innerHTML = `<span class="placeholder">Committed ${r.rev.slice(0,10)} on ${r.branch}. Merge with: git merge ${r.branch}</span>`;
      $("btn-commit").style.display = "none";
    } catch (e) { alert("commit failed: " + e.message); }
    finally { $("btn-commit").disabled = false; }
  };

  const canvas = $("dag");
  const ctx = canvas.getContext("2d");
  let pulse = 0;

  function layout(plan, W, H) {
    const nodes = plan.nodes;
    const depth = {};
    const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
    const get = (id) => {
      if (depth[id] !== undefined) return depth[id];
      const n = byId[id];
      depth[id] = n.depends_on.length ? 1 + Math.max(...n.depends_on.map(get)) : 0;
      return depth[id];
    };
    nodes.forEach(n => get(n.id));
    const levels = {};
    nodes.forEach(n => { const d = depth[n.id]; (levels[d] = levels[d] || []).push(n); });
    const ds = Object.keys(levels).map(Number).sort((a, b) => a - b);
    const pos = {};
    const levelH = ds.length > 1 ? (H - PAD_Y * 2 - NODE_H) / (ds.length - 1) : 0;
    ds.forEach((d, i) => {
      const row = levels[d];
      const rowW = row.length * NODE_W + (row.length - 1) * 40;
      row.forEach((n, j) => { pos[n.id] = { x: (W - rowW) / 2 + j * (NODE_W + 40), y: ds.length > 1 ? PAD_Y + i * levelH : (H - NODE_H) / 2 }; });
    });
    return pos;
  }

  function rr(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (!W || !H) return;
    if (canvas.width !== W * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!state.plan) return;
    const pos = layout(state.plan, W, H);

    for (const n of state.plan.nodes) {
      for (const d of n.depends_on) {
        const a = pos[d], b = pos[n.id];
        const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H;
        const x2 = b.x + NODE_W / 2, y2 = b.y;
        const active = state.nodeState[d] === "done" || state.nodeState[d] === "verified";
        ctx.strokeStyle = active ? PALETTES[theme()].edgeActive : PALETTES[theme()].edge;
        ctx.lineWidth = active ? 1.8 : 1.2;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.bezierCurveTo(x1, y1 + (y2 - y1) / 2, x2, y2 - (y2 - y1) / 2, x2, y2 - 8);
        ctx.stroke();
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath();
        ctx.moveTo(x2, y2 - 2); ctx.lineTo(x2 - 5, y2 - 10); ctx.lineTo(x2 + 5, y2 - 10);
        ctx.closePath(); ctx.fill();
      }
    }

    for (const n of state.plan.nodes) {
      const p = pos[n.id];
      const st = state.nodeState[n.id] || "queued";
      const c = COLORS[st];
      if (st === "running" || st === "waiting") {
        const g = 0.5 + 0.5 * Math.sin(pulse / 14);
        ctx.shadowColor = c.stroke; ctx.shadowBlur = 12 + 14 * g;
      }
      rr(p.x, p.y, NODE_W, NODE_H, 13);
      ctx.fillStyle = c.fill; ctx.fill();
      ctx.strokeStyle = c.stroke; ctx.lineWidth = 1.6; ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.font = "600 8.5px Inter, sans-serif";
      ctx.fillStyle = c.text;
      ctx.globalAlpha = 0.85;
      ctx.fillText((n.kind + (n.approval ? " · gate" : "")).toUpperCase(), p.x + 13, p.y + 17);
      ctx.globalAlpha = 1;
      ctx.font = "11.5px Inter, sans-serif";
      ctx.fillStyle = PALETTES[theme()].nodeText;
      wrapText(n.task, p.x + 13, p.y + 33, NODE_W - 26, 14, 2);
      ctx.beginPath();
      ctx.arc(p.x + NODE_W - 14, p.y + 14, 4, 0, Math.PI * 2);
      ctx.fillStyle = c.stroke;
      ctx.fill();
    }
  }

  function wrapText(text, x, y, maxW, lh, maxLines) {
    const words = String(text).split(" ");
    let line = "", lines = 0;
    for (let i = 0; i < words.length; i++) {
      const test = line ? line + " " + words[i] : words[i];
      if (ctx.measureText(test).width > maxW && line) {
        if (++lines >= maxLines) { ctx.fillText(line.replace(/\s\S*$/, "") + "…", x, y); return; }
        ctx.fillText(line, x, y);
        line = words[i]; y += lh;
      } else line = test;
    }
    if (line && lines < maxLines) ctx.fillText(line, x, y);
  }

  function tick() { pulse++; if (Object.values(state.nodeState).some(s => s === "running" || s === "waiting")) draw(); requestAnimationFrame(tick); }
  window.addEventListener("resize", draw);

  $("launch").onclick = async () => {
    const goal = $("goal").value.trim();
    if (!goal) return;
    $("launch").disabled = true;
    try {
      const { id } = await api("/api/jobs", { body: { goal, provider: $("provider").value, budget: +$("budget").value || 60000, workspace: $("worktree").checked } });
      $("goal").value = "";
      await refreshJobs();
      selectJob(id);
    } catch (e) { alert("launch failed: " + e.message); }
    finally { $("launch").disabled = false; }
  };
  $("goal").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("launch").click(); });

  document.documentElement.dataset.theme = localStorage.getItem("helix-theme") || "light";
  $("theme-toggle").onclick = () => {
    const next = theme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("helix-theme", next);
    if (termCtl.term) termCtl.term.options.theme = TERM_THEMES[next];
    draw();
  };

  /* ---- Embedded terminal: per-job shell over websocket ---- */
  const termCtl = { term: null, fit: null, ws: null, sessions: {}, current: null };
  const TERM_THEMES = {
    light: { background: "#0b0f16", foreground: "#d6deeb", cursor: "#5eead4", selectionBackground: "#1d3b53" },
    dark:  { background: "#0b0f16", foreground: "#d6deeb", cursor: "#5eead4", selectionBackground: "#1d3b53" },
  };

  function ensureXterm() {
    if (termCtl.term) return;
    termCtl.term = new Terminal({
      fontFamily: "ui-monospace, 'JetBrains Mono', Consolas, monospace",
      fontSize: 12, cursorBlink: true, scrollback: 5000,
      theme: TERM_THEMES[theme()],
    });
    termCtl.fit = new FitAddon.FitAddon();
    termCtl.term.loadAddon(termCtl.fit);
    termCtl.term.open($("term-host"));
    termCtl.term.onData(d => { if (termCtl.ws && termCtl.ws.readyState === 1) termCtl.ws.send(JSON.stringify({ type: "input", data: d })); });
    new ResizeObserver(() => {
      if ($("terminal").style.display === "none") return;
      termCtl.fit.fit();
      if (termCtl.ws && termCtl.ws.readyState === 1)
        termCtl.ws.send(JSON.stringify({ type: "resize", cols: termCtl.term.cols, rows: termCtl.term.rows }));
    }).observe($("term-host"));
  }

  async function openTerminal() {
    if (!state.jobId) return;
    ensureXterm();
    const known = termCtl.sessions[state.jobId];
    if (known) { attachTerminal(known); return; }
    try {
      const s = await api(`/api/jobs/${state.jobId}/terminal`, { body: {} });
      termCtl.sessions[state.jobId] = s;
      attachTerminal(s);
    } catch (e) {
      termCtl.term.write("\r\n[terminal unavailable: " + e.message + "]\r\n");
    }
  }

  function attachTerminal(s) {
    if (termCtl.current === s.id && termCtl.ws && termCtl.ws.readyState <= 1) { termCtl.fit.fit(); return; }
    if (termCtl.ws) termCtl.ws.close();
    termCtl.current = s.id;
    $("term-cwd").textContent = s.cwd;
    termCtl.term.reset();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/terminal/${s.id}/ws`);
    termCtl.ws = ws;
    ws.onopen = () => {
      termCtl.fit.fit();
      ws.send(JSON.stringify({ type: "resize", cols: termCtl.term.cols, rows: termCtl.term.rows }));
    };
    ws.onmessage = (m) => {
      const f = JSON.parse(m.data);
      if (f.type === "output") termCtl.term.write(f.data);
      else if (f.type === "exit") { termCtl.term.write("\r\n[shell exited - use New shell to restart]\r\n"); delete termCtl.sessions[state.jobId]; }
    };
    ws.onclose = (e) => {
      if (e.code === 4404) { delete termCtl.sessions[state.jobId]; termCtl.term.write("\r\n[session expired - click Terminal again for a fresh shell]\r\n"); }
    };
  }

  $("btn-term-restart").onclick = async () => {
    const s = termCtl.sessions[state.jobId];
    if (s) { try { await fetch(`/api/terminal/${s.id}`, { method: "DELETE" }); } catch (e) {} delete termCtl.sessions[state.jobId]; }
    termCtl.current = null;
    openTerminal();
  };

  /* ---- Jobs board: every run at a glance ---- */
  const board = { visible: false, timer: null, modalJob: null };

  function fmtAge(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  function showBoard(on) {
    board.visible = on;
    $("board-view").style.display = on ? "" : "none";
    $("console-view").style.display = on ? "none" : "";
    $("btn-board").textContent = on ? "Console" : "Board";
    $("btn-board").classList.toggle("on", on);
    clearInterval(board.timer); board.timer = null;
    if (on) { refreshBoard(); board.timer = setInterval(refreshBoard, 4000); }
  }
  $("btn-board").onclick = () => showBoard(!board.visible);

  async function refreshBoard() {
    let jobs;
    try { ({ jobs } = await api("/api/jobs?limit=48")); } catch (e) { return; }
    const grid = $("board-grid");
    grid.innerHTML = "";
    if (!jobs.length) {
      grid.innerHTML = '<div class="bcard-empty">No jobs yet - launch one from the console.</div>';
      return;
    }
    for (const j of jobs) grid.appendChild(boardCard(j));
    // diff stats for worktree jobs, lazily (cheap --stat only)
    for (const j of jobs) {
      if (!j.workspace_path) continue;
      api(`/api/jobs/${j.id}/diff?stat=1`).then(d => {
        const el = document.getElementById("stat-" + j.id);
        if (!el) return;
        const lines = (d.diff || "").trim().split("\n").filter(Boolean);
        const summary = lines.length ? lines[lines.length - 1] : "";
        el.textContent = summary && /file|insertion|deletion/.test(summary) ? summary : "workspace clean";
      }).catch(() => {});
    }
  }

  function boardCard(j) {
    const el = document.createElement("div");
    el.className = "bcard";
    const pct = j.nodes_total ? Math.round(100 * (j.nodes_done || 0) / j.nodes_total) : 0;
    el.innerHTML = `
      <div class="bcard-top">
        <span class="pill ${j.status}">${j.status.replace("_", " ")}</span>
        <span class="bcard-meta">${fmtAge(j.created_at)}</span>
      </div>
      <div class="bcard-goal"></div>
      ${j.nodes_total ? `<div class="bcard-progress" title="${j.nodes_done || 0} of ${j.nodes_total} nodes done"><div style="width:${pct}%"></div></div>` : ""}
      <div class="bcard-meta">
        <span>${j.provider || ""}</span>
        <span>${j.nodes_total ? (j.nodes_done || 0) + "/" + j.nodes_total + " nodes" : "no plan yet"}</span>
        <span>${fmtNum(j.tokens_used || 0)} tok</span>
        <span>$${(j.cost_usd || 0).toFixed(2)}</span>
      </div>
      ${j.workspace_branch ? `<div class="bcard-branch" title="${j.workspace_branch}">&#9095; ${j.workspace_branch}</div><div class="bcard-stat" id="stat-${j.id}"></div>` : ""}
      <div class="bcard-actions">
        <button class="b-open">Open</button>
        ${j.workspace_path ? '<button class="b-diff">Changes</button>' : ""}
      </div>`;
    el.querySelector(".bcard-goal").textContent = j.goal;
    el.onclick = () => { showBoard(false); selectJob(j.id); };
    const diffBtn = el.querySelector(".b-diff");
    if (diffBtn) diffBtn.onclick = (e) => { e.stopPropagation(); openDiffModal(j); };
    return el;
  }

  async function openDiffModal(j) {
    board.modalJob = j;
    $("diff-modal-title").textContent = j.goal.slice(0, 90) + " - " + (j.workspace_branch || "");
    $("diff-modal-body").innerHTML = '<span class="placeholder">loading diff...</span>';
    $("diff-modal-commit").style.display = "none";
    $("diff-modal").style.display = "";
    try {
      const d = await api(`/api/jobs/${j.id}/diff`);
      $("diff-modal-body").innerHTML = renderDiff(d.diff);
      $("diff-modal-commit").style.display = (d.diff && d.diff.trim()) ? "" : "none";
    } catch (e) {
      $("diff-modal-body").textContent = "no diff available: " + e.message;
    }
  }
  function closeDiffModal() { $("diff-modal").style.display = "none"; board.modalJob = null; }
  $("diff-modal-close").onclick = closeDiffModal;
  $("diff-modal").onclick = (e) => { if (e.target === $("diff-modal")) closeDiffModal(); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && board.modalJob) closeDiffModal(); });
  $("diff-modal-commit").onclick = async () => {
    if (!board.modalJob) return;
    $("diff-modal-commit").disabled = true;
    try {
      const r = await api(`/api/jobs/${board.modalJob.id}/commit`, { body: {} });
      $("diff-modal-body").innerHTML = `<span class="placeholder">Committed ${r.rev.slice(0,10)} on ${r.branch}. Merge with: git merge ${r.branch}</span>`;
      $("diff-modal-commit").style.display = "none";
      refreshBoard();
    } catch (e) { alert("commit failed: " + e.message); }
    finally { $("diff-modal-commit").disabled = false; }
  };

  refreshStats(); refreshJobs(); tick();
  const params = new URLSearchParams(location.search);
  if (params.get("board") === "1") showBoard(true);
  const wanted = params.get("job");
  if (wanted) { showBoard(false); selectJob(wanted); }
  setInterval(() => { refreshStats(); refreshJobs(); }, 8000);
})();
