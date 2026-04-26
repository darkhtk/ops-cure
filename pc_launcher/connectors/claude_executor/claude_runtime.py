"""Spawn and feed the claude CLI in --input-format stream-json mode.

This is the Python port of claude-remote/src/runtime-claude.js. We keep
each spawned `claude` process alive across multi-turn interactions
(stream-json holds the conversation in one process). When the agent
processes a `run.start`, it spawns a new process; subsequent `run.input`
commands write a fresh user-message envelope to the same process's stdin.

stdout JSON lines are dispatched to a callback; the agent forwards them
to the bridge via /agent/events.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Callable


CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")


def _resolve_claude_bin() -> str:
    """Pick the claude executable. Honours $CLAUDE_BIN, falls back to PATH
    lookup. Keeps the npm wrapper (`.cmd` on Windows) when available.
    """
    bin_env = os.getenv("CLAUDE_BIN")
    if bin_env and os.path.exists(bin_env):
        return bin_env
    found = shutil.which(CLAUDE_BIN)
    if found:
        return found
    if os.name == "nt":
        cmd = shutil.which(f"{CLAUDE_BIN}.cmd")
        if cmd:
            return cmd
    return CLAUDE_BIN  # let subprocess fail if missing


class ClaudeRun:
    """One live claude child process. Owned by the agent for the duration
    of a session — kept alive across multi-turn interactions.
    """

    def __init__(
        self,
        *,
        cwd: str,
        permission_mode: str | None = None,
        model: str | None = None,
        on_event: Callable[[dict[str, Any]], None],
        on_exit: Callable[[int | None], None] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.model = model
        self.on_event = on_event
        self.on_exit = on_exit
        self._extra_args = extra_args or []
        self.session_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._closed = False

    # -------- lifecycle ---------------------------------------------------

    def spawn(self) -> None:
        bin_path = _resolve_claude_bin()
        args = [
            bin_path,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self.model:
            args += ["--model", self.model]
        if self.permission_mode and self.permission_mode != "default":
            args += ["--permission-mode", self.permission_mode]
        args += self._extra_args
        self._proc = subprocess.Popen(
            args,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        self._wait_thread = threading.Thread(target=self._wait_for_exit, daemon=True)
        self._wait_thread.start()

    def write_user_message(self, text: str, attachments: list[dict[str, Any]] | None = None) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.closed:
            raise RuntimeError("claude run not alive")
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for att in attachments or []:
            if not isinstance(att, dict):
                continue
            mime = str(att.get("mimeType") or "").lower()
            data = att.get("dataBase64") or att.get("bytesBase64") or att.get("data") or ""
            if not data or not mime.startswith("image/"):
                continue
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": str(data)},
            })
        if not content:
            content.append({"type": "text", "text": ""})
        envelope = {"type": "user", "message": {"role": "user", "content": content}}
        line = json.dumps(envelope) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise RuntimeError(f"write to claude stdin failed: {e}") from e

    def interrupt(self) -> None:
        if self._proc is None:
            return
        try:
            # SIGINT-equivalent for the child claude process. On Windows we
            # send a CTRL_BREAK_EVENT-like terminate; SIGINT itself is POSIX.
            if os.name == "nt":
                self._proc.terminate()
            else:
                self._proc.send_signal(2)
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc is not None and self._proc.stdin and not self._proc.stdin.closed:
            try: self._proc.stdin.close()
            except Exception: pass
        try:
            if self._proc is not None:
                self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: self._proc.kill()
            except Exception: pass

    # -------- internals ---------------------------------------------------

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                # Forward as raw text via a sentinel kind so the bridge can
                # still surface it (debug / parse-error visibility).
                self._safe_emit({"kind": "claude.parse_error", "raw": line[:1024]})
                continue
            if not isinstance(event, dict):
                continue
            # Capture session_id from the system/init event so the agent
            # can record it on the bridge.
            if (
                event.get("type") == "system"
                and event.get("subtype") == "init"
                and isinstance(event.get("session_id"), str)
                and not self.session_id
            ):
                self.session_id = event["session_id"]
            self._safe_emit({"kind": "claude.event", "event": event})

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw_line in proc.stderr:
            line = raw_line.rstrip("\n").rstrip("\r")
            if line:
                self._safe_emit({"kind": "claude.stderr", "text": line})

    def _wait_for_exit(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        self._safe_emit({"kind": "claude.exit", "code": code})
        if self.on_exit is not None:
            try: self.on_exit(code)
            except Exception: pass

    def _safe_emit(self, payload: dict[str, Any]) -> None:
        try:
            self.on_event(payload)
        except Exception:
            pass
