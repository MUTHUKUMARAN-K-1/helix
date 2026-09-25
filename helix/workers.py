"""Worker adapters: hand a DAG node to an external coding agent.

The preset registry below maps well-known coding-agent CLIs to their
headless invocation. `http` covers any remote OpenAI-compatible agent
endpoint (env HELIX_WORKER_URL) and `custom` any CLI template via env
HELIX_WORKER_CMD, e.g. HELIX_WORKER_CMD="my-agent run --task {prompt}".

A worker node keeps Helix's guarantees: the executor still verifies the
worker's output, still retries, and approval gates still hold.

MCP: when <db dir>/mcp.json exists it is passed to workers that accept an
MCP config (claude-code via --mcp-config; every worker also gets the path
in HELIX_MCP_CONFIG). Manage the file with `helix mcp`.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from dataclasses import dataclass

# Preset registry for well-known coding agents (extend as CLIs ship headless modes).
# {prompt} is replaced with the node prompt; {mcp} with extra MCP args (or removed).
PRESETS = {
    "claude-code": {"bin": "claude",
                    "argv": ["claude", "-p", "{prompt}", "--output-format", "text", "{mcp}"]},
    "codex": {"bin": "codex", "argv": ["codex", "exec", "{prompt}"]},
    "gemini-cli": {"bin": "gemini", "argv": ["gemini", "-p", "{prompt}"]},
    "aider": {"bin": "aider", "argv": ["aider", "--message", "{prompt}", "--yes-always"]},
    "goose": {"bin": "goose", "argv": ["goose", "run", "-t", "{prompt}"]},
    "opencode": {"bin": "opencode", "argv": ["opencode", "run", "{prompt}"]},
}

WORKERS = tuple(PRESETS) + ("http", "custom")


@dataclass
class WorkerResult:
    text: str
    worker: str


class WorkerUnavailable(RuntimeError):
    pass


def available_workers() -> dict[str, bool]:
    out = {name: shutil.which(p["bin"]) is not None for name, p in PRESETS.items()}
    out["http"] = bool(os.environ.get("HELIX_WORKER_URL"))
    out["custom"] = bool(os.environ.get("HELIX_WORKER_CMD"))
    return out


def _mcp_env_and_args(mcp_config: str | None) -> tuple[dict, str]:
    """Passthrough: every worker learns the config path; claude-code also gets the flag."""
    if not mcp_config or not os.path.isfile(mcp_config):
        return {}, ""
    return {"HELIX_MCP_CONFIG": mcp_config}, f"--mcp-config {mcp_config}"


async def run_worker(name: str, prompt: str, cwd: str | None = None,
                     timeout_s: int = 1800, mcp_config: str | None = None) -> WorkerResult:
    if name in PRESETS:
        preset = PRESETS[name]
        if not shutil.which(preset["bin"]):
            raise WorkerUnavailable(
                f"{preset['bin']} CLI not found on PATH (install {name} first)")
        env_extra, mcp_args = _mcp_env_and_args(mcp_config)
        argv = []
        for a in preset["argv"]:
            a = a.replace("{prompt}", prompt).replace("{mcp}", mcp_args)
            argv.extend(shlex.split(a))
        return await _cli(name, argv, cwd, timeout_s, env_extra=env_extra)
    if name == "custom":
        tmpl = os.environ.get("HELIX_WORKER_CMD")
        if not tmpl:
            raise WorkerUnavailable("HELIX_WORKER_CMD is not set")
        argv = shlex.split(tmpl)
        if any("{prompt}" in a for a in argv):
            argv = [a.replace("{prompt}", prompt) for a in argv]
        else:
            argv.append(prompt)
        return await _cli(name, argv, cwd, timeout_s)
    if name == "http":
        return await _http(name, prompt, timeout_s)
    raise WorkerUnavailable(f"unknown worker {name!r}; choose from {WORKERS}")


async def _cli(name: str, argv: list[str], cwd: str | None, timeout_s: int) -> WorkerResult:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        raise WorkerUnavailable(f"{name} timed out after {timeout_s}s")
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise WorkerUnavailable(f"{name} exited {proc.returncode}: {err.decode(errors='replace')[:400]}")
    if not text:
        raise WorkerUnavailable(f"{name} produced no output")
    return WorkerResult(text=text, worker=name)


async def _http(name: str, prompt: str, timeout_s: int) -> WorkerResult:
    import httpx
    url = os.environ.get("HELIX_WORKER_URL", "").rstrip("/")
    if not url:
        raise WorkerUnavailable("HELIX_WORKER_URL is not set")
    key = os.environ.get("HELIX_WORKER_KEY", "")
    model = os.environ.get("HELIX_WORKER_MODEL", "default")
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            f"{url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]})
        resp.raise_for_status()
        data = resp.json()
    return WorkerResult(text=data["choices"][0]["message"]["content"], worker=name)
