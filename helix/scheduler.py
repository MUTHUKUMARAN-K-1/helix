"""Cron scheduler: run goals or playbooks on a schedule.

Schedules live in ``<db dir>/schedules.json``. A small foreground daemon
(``helix schedule daemon``) wakes every 30s, finds due entries and launches
jobs through the normal runner - so scheduled runs get the same event log,
memory, gates and budget controls as interactive ones.

Cron expressions are standard 5-field: ``minute hour day-of-month month
day-of-week`` with ``*``, ``*/n``, lists (``1,2``) and ranges (``1-5``).
Times are local to the machine running the daemon.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


class ScheduleError(ValueError):
    pass


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"empty cron field part in {spec!r}")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ScheduleError("cron step must be >= 1")
        if part == "*":
            rng = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            rng = range(int(part), int(part) + 1)
        vals = [v for v in rng if lo <= v <= hi]
        if not vals and part != "*":
            raise ScheduleError(f"cron value out of range in {spec!r}")
        out.update(vals[::step] if part == "*" or "-" in part else vals)
    return out


class CronExpr:
    """Parsed 5-field cron expression with minute-resolution matching."""

    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ScheduleError(
                f"cron expression needs 5 fields (minute hour dom month dow), got {expr!r}")
        self.minutes = _parse_field(fields[0], 0, 59)
        self.hours = _parse_field(fields[1], 0, 23)
        self.dom = _parse_field(fields[2], 1, 31)
        self.months = _parse_field(fields[3], 1, 12)
        # accept 7 as Sunday alongside 0
        self.dow = {0 if v == 7 else v for v in _parse_field(fields[4], 0, 7)}
        self.raw = expr

    def matches(self, tm: time.struct_time) -> bool:
        cron_dow = (tm.tm_wday + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6
        return (tm.tm_min in self.minutes and tm.tm_hour in self.hours
                and tm.tm_mday in self.dom and tm.tm_mon in self.months
                and cron_dow in self.dow)


@dataclass
class Schedule:
    name: str
    cron: str
    goal: str = ""
    playbook: str = ""
    provider: str = "mock"
    budget: int = 60000
    enabled: bool = True
    last_run_minute: str = ""  # "YYYY-MM-DD HH:MM" - idempotency guard


class ScheduleStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "schedules.json"
        if not self.file.exists():
            self._write([])

    def _read(self) -> list[dict]:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def _write(self, rows: list[dict]):
        self.file.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def list(self) -> list[Schedule]:
        return [Schedule(**r) for r in self._read()]

    def add(self, sched: Schedule):
        CronExpr(sched.cron)  # validate
        if not sched.goal and not sched.playbook:
            raise ScheduleError("schedule needs a --goal or a --playbook")
        rows = [r for r in self._read() if r["name"] != sched.name]
        rows.append(asdict(sched))
        self._write(rows)

    def remove(self, name: str) -> bool:
        rows = self._read()
        kept = [r for r in rows if r["name"] != name]
        self._write(kept)
        return len(kept) != len(rows)

    def mark_run(self, name: str, minute: str):
        rows = self._read()
        for r in rows:
            if r["name"] == name:
                r["last_run_minute"] = minute
        self._write(rows)

    def due(self, now: float | None = None) -> list[Schedule]:
        tm = time.localtime(now or time.time())
        minute = time.strftime("%Y-%m-%d %H:%M", tm)
        out = []
        for s in self.list():
            if not s.enabled or s.last_run_minute == minute:
                continue
            if CronExpr(s.cron).matches(tm):
                out.append(s)
        return out


# ---------------- inbound webhooks ----------------

@dataclass
class Webhook:
    name: str
    token: str
    goal: str = ""
    playbook: str = ""
    provider: str = "mock"
    budget: int = 60000
    enabled: bool = True


class WebhookStore:
    """Named inbound hooks: POST /api/hooks/<token> fires the goal/playbook."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "webhooks.json"
        if not self.file.exists():
            self._write([])

    def _read(self) -> list[dict]:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def _write(self, rows: list[dict]):
        self.file.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def list(self) -> list[Webhook]:
        return [Webhook(**r) for r in self._read()]

    def add(self, hook: Webhook):
        import secrets
        if not hook.token:
            hook.token = secrets.token_hex(8)
        if not hook.goal and not hook.playbook:
            raise ScheduleError("webhook needs a --goal or a --playbook")
        rows = [r for r in self._read() if r["name"] != hook.name]
        rows.append(asdict(hook))
        self._write(rows)
        return hook.token

    def by_token(self, token: str) -> Webhook | None:
        for h in self.list():
            if h.enabled and h.token == token:
                return h
        return None

    def remove(self, name: str) -> bool:
        rows = self._read()
        kept = [r for r in rows if r["name"] != name]
        self._write(kept)
        return len(kept) != len(rows)
