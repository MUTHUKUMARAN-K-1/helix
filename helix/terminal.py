"""Embedded terminal sessions: PTY-backed shells bridged to websockets.

POSIX uses the stdlib pty module. Windows uses pywinpty (installed
automatically on that platform). A session owns a shell process whose
cwd is the job's worktree when it has one, else the server's cwd.
Sessions survive websocket disconnects so a dashboard refresh does not
kill the shell; an idle reaper cleans up sessions with no client left.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    try:
        from winpty import PtyProcess  # pywinpty
    except ImportError:  # pragma: no cover - depends on platform
        PtyProcess = None
else:
    PtyProcess = None

READ_CHUNK = 65536
SCROLLBACK_LIMIT = 256 * 1024  # bytes kept for re-attach replay
IDLE_TIMEOUT_S = 30 * 60


def default_shell() -> str:
    if IS_WINDOWS:
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"


class PTYSession:
    """One shell on a pseudo-terminal, with a thread pumping output."""

    def __init__(self, cwd: str, shell: Optional[str] = None, cols: int = 100, rows: int = 28):
        self.id = "term_" + uuid.uuid4().hex[:10]
        self.cwd = cwd
        self.shell = shell or default_shell()
        self.cols, self.rows = cols, rows
        self.created_at = time.time()
        self.last_attach = time.time()
        self.exit_code: Optional[int] = None
        self._scrollback = bytearray()
        self._listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()
        self._closed = False
        self._spawn()

    # ---- platform spawn / io ----
    def _spawn(self):
        env = dict(os.environ, TERM="xterm-256color", HELIX_TERMINAL="1")
        if IS_WINDOWS:
            if PtyProcess is None:
                raise RuntimeError("pywinpty is required for terminals on Windows (pip install pywinpty)")
            self._proc = PtyProcess.spawn(self.shell, cwd=self.cwd or None,
                                          dimensions=(self.rows, self.cols), env=env)
            self._master = None
        else:
            import fcntl
            import pty
            import termios
            master, slave = pty.openpty()
            winsz = __import__("struct").pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, winsz)
            self._proc = subprocess.Popen(
                [self.shell], stdin=slave, stdout=slave, stderr=slave,
                cwd=self.cwd or None, env=env, start_new_session=True,
                close_fds=True)
            os.close(slave)
            os.set_blocking(master, False)
            self._master = master
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        try:
            while not self._closed:
                data = self._read_chunk()
                if not data:
                    if not self.alive:
                        break
                    time.sleep(0.02)
                    continue
                with self._lock:
                    self._scrollback.extend(data)
                    if len(self._scrollback) > SCROLLBACK_LIMIT:
                        del self._scrollback[: len(self._scrollback) - SCROLLBACK_LIMIT]
                    listeners = list(self._listeners)
                for loop, q in listeners:
                    if loop is None:
                        self._offer(q, data)
                        continue
                    try:
                        loop.call_soon_threadsafe(self._offer, q, data)
                    except RuntimeError:
                        pass  # client loop is gone
        finally:
            code = self._returncode()
            self.exit_code = code if code is not None else 0
            with self._lock:
                listeners = list(self._listeners)
            for loop, q in listeners:
                if loop is None:
                    self._offer(q, None)  # EOF sentinel
                    continue
                try:
                    loop.call_soon_threadsafe(self._offer, q, None)
                except RuntimeError:
                    pass

    def _read_chunk(self) -> bytes:
        if IS_WINDOWS:
            # pywinpty 3.x read() has no non-blocking mode: it blocks on a
            # socket recv until data arrives and raises EOFError at exit.
            # Blocking here is fine - this runs on the session's pump thread
            # and kill() closes the socket, which wakes it.
            try:
                chunk = self._proc.read(READ_CHUNK)
            except EOFError:
                return b""
            except OSError:
                return b""
            return chunk.encode("utf-8", "replace") if isinstance(chunk, str) else (chunk or b"")
        try:
            return os.read(self._master, READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            return b""

    def _returncode(self) -> Optional[int]:
        if IS_WINDOWS:
            return self._proc.exitstatus
        return self._proc.poll()

    @property
    def alive(self) -> bool:
        return self._returncode() is None

    # ---- client interface ----
    def write(self, data: bytes):
        if not self.alive:
            return
        if IS_WINDOWS:
            self._proc.write(data.decode("utf-8", "replace"))
        else:
            try:
                os.write(self._master, data)
            except OSError:
                pass

    def resize(self, cols: int, rows: int):
        cols = max(2, min(cols, 500))
        rows = max(2, min(rows, 200))
        self.cols, self.rows = cols, rows
        if IS_WINDOWS:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass
        else:
            try:
                import fcntl
                import struct
                import termios
                fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    @staticmethod
    def _offer(q: asyncio.Queue, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def attach(self) -> tuple[asyncio.Queue, bytes]:
        """Subscribe a client; returns (queue, scrollback to replay).

        Must be called from the client's event loop: the reader thread
        wakes consumers through that loop, so cross-thread puts arrive.
        """
        self.last_attach = time.time()
        try:
            loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # sync consumer (tests, scripts): direct puts
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._listeners.append((loop, q))
            snapshot = bytes(self._scrollback)
        return q, snapshot

    def detach(self, q: asyncio.Queue):
        self.last_attach = time.time()
        with self._lock:
            self._listeners = [(l, x) for (l, x) in self._listeners if x is not q]

    @property
    def listener_count(self) -> int:
        with self._lock:
            return len(self._listeners)

    def kill(self):
        self._closed = True
        try:
            if IS_WINDOWS:
                self._proc.terminate(force=True)
            else:
                os.killpg(self._proc.pid, signal.SIGHUP)
                self._proc.terminate()
        except Exception:
            pass
        if not IS_WINDOWS and self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass


class TerminalManager:
    def __init__(self, base_cwd: str = "."):
        self.base_cwd = str(Path(base_cwd).resolve())
        self._sessions: dict[str, PTYSession] = {}
        self._lock = threading.Lock()

    def create(self, cwd: Optional[str] = None, cols: int = 100, rows: int = 28) -> PTYSession:
        target = cwd or self.base_cwd
        if not os.path.isdir(target):
            target = self.base_cwd
        sess = PTYSession(cwd=target, cols=cols, rows=rows)
        with self._lock:
            self._sessions[sess.id] = sess
        return sess

    def sessions(self) -> list[PTYSession]:
        with self._lock:
            return list(self._sessions.values())

    def get(self, sid: str) -> Optional[PTYSession]:
        with self._lock:
            return self._sessions.get(sid)

    def kill(self, sid: str) -> bool:
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if not sess:
            return False
        sess.kill()
        return True

    def reap_idle(self):
        """Drop dead or long-detached sessions."""
        now = time.time()
        with self._lock:
            doomed = [sid for sid, s in self._sessions.items()
                      if (not s.alive and s.listener_count == 0)
                      or (s.listener_count == 0 and now - s.last_attach > IDLE_TIMEOUT_S)]
            for sid in doomed:
                self._sessions.pop(sid).kill()
