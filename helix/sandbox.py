"""Sandboxed shell execution for kind="exec" nodes.

Direct subprocess execution with a guard: the allowlist in
HELIX_EXEC_ALLOW (comma-separated command prefixes) decides what may run.
Default allowlist is conservative build/test/read commands. No Docker
required; the guard + the event log give you the audit trail.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass

# Unix default; Windows gets a cmd/PowerShell-flavored list (python -c stays
# the portable way to write files - redirects are blocked on every OS).
_DEFAULT_ALLOW_POSIX = "python3,python,pip,node,npm,npx,pytest,cat,ls,echo,grep,find,jq,curl,git status,git diff,git log"
_DEFAULT_ALLOW_NT = "python,python3,py,pip,node,npm,npx,pytest,echo,dir,type,findstr,curl,git status,git diff,git log"
DEFAULT_ALLOW = _DEFAULT_ALLOW_NT if os.name == "nt" else _DEFAULT_ALLOW_POSIX


@dataclass
class ExecResult:
    text: str
    worker: str = "exec"


class ExecBlocked(RuntimeError):
    pass


def _allowlist() -> list[str]:
    raw = os.environ.get("HELIX_EXEC_ALLOW", DEFAULT_ALLOW)
    return [x.strip() for x in raw.split(",") if x.strip()]


# Shell constructs that would let one allowlisted command smuggle in another.
_META = ("&&", "||", ";", "`", "$(", "${", "\n", ">", "<")


def _segment_allowed(cmd: str, allow: list[str]) -> bool:
    for prefix in allow:
        if cmd == prefix or cmd.startswith(prefix + " "):
            return True
    return False


def allowed(command: str) -> bool:
    cmd = " ".join(command.strip().split())
    if any(m in cmd for m in _META):
        return False
    allow = _allowlist()
    # Pipelines are fine when every segment is allowlisted (e.g. cat x | jq .)
    return all(_segment_allowed(seg.strip(), allow) for seg in cmd.split("|"))


async def run_exec(command: str, timeout_s: int = 300, cwd: str | None = None) -> ExecResult:
    if not allowed(command):
        raise ExecBlocked(
            f"command not in HELIX_EXEC_ALLOW allowlist: {command!r}. "
            f"Allowed prefixes: {_allowlist()}")
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        raise ExecBlocked(f"exec timed out after {timeout_s}s: {command!r}")
    text = out.decode(errors="replace")
    return ExecResult(text=f"$ {command}\n(exit {proc.returncode})\n\n{text[-6000:]}")
