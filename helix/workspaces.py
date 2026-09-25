"""Job workspaces: every coding job can run in its own git worktree.

A workspace is a git worktree of the current repository on branch
``helix/<job_id>``, created under ``.helix/workspaces/<job_id>/``.
Exec and agent nodes run inside it, so whatever a job changes stays
isolated from the user's checkout - and the result is a branch with a
clean diff the user can review, approve and merge.

Non-git projects fall back to a plain directory copy-free workspace:
nodes still get a private cwd, there is just no branch/diff.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def in_git_repo(path: str) -> bool:
    return _git(["rev-parse", "--is-inside-work-tree"], cwd=path).returncode == 0


@dataclass
class Workspace:
    job_id: str
    path: Path
    branch: str = ""       # empty when the base dir is not a git repo
    base: str = ""         # repo root the worktree belongs to

    @property
    def is_git(self) -> bool:
        return bool(self.branch)


class WorkspaceManager:
    def __init__(self, base_dir: str):
        self.base = Path(base_dir).resolve()
        self.root = self.base / ".helix" / "workspaces"

    def create(self, job_id: str) -> Workspace:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / job_id
        branch = f"helix/{job_id}"
        if in_git_repo(str(self.base)):
            # ignored by default: keep workspaces out of the parent's status
            exclude = self.base / ".git" / "info" / "exclude"
            try:
                line = ".helix/workspaces/"
                existing = exclude.read_text() if exclude.exists() else ""
                if line not in existing:
                    exclude.parent.mkdir(parents=True, exist_ok=True)
                    exclude.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "") + line + "\n")
            except OSError:
                pass
            res = _git(["worktree", "add", "-b", branch, str(path)], cwd=str(self.base))
            if res.returncode != 0:
                # branch may exist from a crashed run; reuse it
                res = _git(["worktree", "add", str(path), branch], cwd=str(self.base))
                if res.returncode != 0:
                    raise WorkspaceError(f"git worktree add failed: {res.stderr.strip()}")
            return Workspace(job_id=job_id, path=path, branch=branch, base=str(self.base))
        path.mkdir(parents=True, exist_ok=True)
        return Workspace(job_id=job_id, path=path)

    def diff(self, ws: Workspace, stat_only: bool = False) -> str:
        if not ws.is_git:
            return ""
        # include untracked files so new work shows up in review
        _git(["add", "-A"], cwd=str(ws.path))
        args = ["diff", "--cached"]
        if stat_only:
            args.append("--stat")
        args.append(f"HEAD")
        res = _git(args, cwd=str(ws.path))
        return res.stdout if res.returncode == 0 else ""

    def commit(self, ws: Workspace, message: str) -> str:
        if not ws.is_git:
            raise WorkspaceError("workspace is not a git worktree; nothing to commit")
        _git(["add", "-A"], cwd=str(ws.path))
        # CI and fresh machines often have no git identity; fall back only then
        ident = [] if _git(["config", "user.email"], cwd=str(ws.path)).stdout.strip() \
            else ["-c", "user.name=Helix", "-c", "user.email=helix@localhost"]
        res = _git([*ident, "commit", "-m", message], cwd=str(ws.path))
        if res.returncode != 0 and "nothing to commit" not in (res.stdout + res.stderr):
            raise WorkspaceError(f"git commit failed: {res.stderr.strip()}")
        rev = _git(["rev-parse", "HEAD"], cwd=str(ws.path))
        return rev.stdout.strip()

    def remove(self, ws: Workspace):
        if ws.is_git:
            _git(["worktree", "remove", "--force", str(ws.path)], cwd=str(self.base))
        elif ws.path.exists():
            shutil.rmtree(ws.path, ignore_errors=True)


def workspace_root_for(db_path: str, job_id: str) -> str:
    """Default base for workspaces when the caller does not pick a project dir."""
    return os.getcwd()
