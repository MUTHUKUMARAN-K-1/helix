"""Markdown memory store with recall.

Learnings written by nodes/jobs live as markdown files under a workspace
``memory/`` directory. Before planning or dispatch, Helix recalls the
memories relevant to the goal and injects them into the prompt context.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[a-zA-Z0-9_]{3,}")
_HEADER = "# Helix memory\n\nLearnings from previous jobs, newest first.\n"


@dataclass
class Memory:
    key: str
    text: str
    ts: float
    source: str = ""


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.learnings_file = self.root / "LEARNINGS.md"
        self.agents_dir = self.root / "agents"
        self.agents_dir.mkdir(exist_ok=True)
        if not self.learnings_file.exists():
            self.learnings_file.write_text(_HEADER, encoding="utf-8")

    # ---------- write ----------
    def remember(self, text: str, source: str = "", key: str | None = None) -> Path:
        key = key or re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-")
        entry = f"\n## {key}\n\n- when: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        if source:
            entry += f"- source: {source}\n"
        entry += f"\n{text}\n"
        with self.learnings_file.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return self.learnings_file

    def remember_for_agent(self, agent: str, text: str) -> Path:
        path = self.agents_dir / f"{agent}.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else f"# Memory for {agent}\n"
        path.write_text(existing + f"\n- {time.strftime('%Y-%m-%d %H:%M:%S')}: {text}\n", encoding="utf-8")
        return path

    # ---------- read / recall ----------
    def _entries(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.learnings_file.exists():
            out.append(("LEARNINGS.md", self.learnings_file.read_text(encoding="utf-8")))
        for p in sorted(self.agents_dir.glob("*.md")):
            out.append((p.name, p.read_text(encoding="utf-8")))
        return out

    def recall(self, query: str, limit: int = 12) -> list[Memory]:
        qwords = set(_WORD.findall(query.lower()))
        if not qwords:
            return []
        scored: list[tuple[int, Memory]] = []
        for fname, body in self._entries():
            for chunk in re.split(r"\n{2,}", body):
                words = set(_WORD.findall(chunk.lower()))
                score = len(qwords & words)
                if score:
                    scored.append((score, Memory(key=fname, text=chunk.strip(), ts=0)))
        scored.sort(key=lambda t: -t[0])
        return [m for _, m in scored[:limit]]

    def recall_text(self, query: str, limit: int = 12, max_chars: int = 4000) -> str:
        mems = self.recall(query, limit=limit)
        out, used = [], 0
        for m in mems:
            if used + len(m.text) > max_chars:
                break
            out.append(m.text)
            used += len(m.text)
        return "\n\n---\n\n".join(out)
