"""Event hooks: run your own scripts on Helix events.

Drop an executable named after an event type (``node_completed``,
``plan_ready``, ...) into ``<db dir>/hooks/`` and Helix runs it with the
event JSON on stdin every time that event fires. Scripts get 5 seconds,
output is ignored, failures never block the job. ``all`` runs on every
event. That is the whole plugin system: any language, no SDK.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HOOK_TIMEOUT_S = 5


def fire_hooks(hooks_dir, event_type: str, payload: dict):
    root = Path(hooks_dir)
    if not root.is_dir():
        return
    for name in (event_type, "all"):
        script = root / name
        if not script.is_file() or not os.access(script, os.X_OK):
            continue
        try:
            subprocess.run(
                [str(script)], input=json.dumps(payload),
                capture_output=True, timeout=HOOK_TIMEOUT_S, text=True)
        except Exception:
            pass  # hooks are best-effort; the event log is the record
