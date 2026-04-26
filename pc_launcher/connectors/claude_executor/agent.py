"""claude_executor agent main loop.

Polls the bridge's /agent/commands/claim, dispatches to the right
handler, reports back the result.

Command types:
  run.start         spawn a new claude process; first user message
  run.input         append a user message to a live run
  run.interrupt     SIGINT the live run
  session.delete    unlink the jsonl
  fs.list           directory listing on this PC
  fs.mkdir          mkdir on this PC
  approval.respond  resolve a pending PreToolUse approval (TODO — needs
                    the approval round-trip to be wired up; F2 supports
                    the queue path but actual claude PreToolUse hook
                    integration is a follow-up).
"""

from __future__ import annotations

import json
import re
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient
from .claude_runtime import ClaudeRun
from .session_sync import find_session_jsonl, scan_sessions


# Command type tokens (must match nas_bridge state_service)
RUN_START = "run.start"
RUN_INPUT = "run.input"
RUN_INTERRUPT = "run.interrupt"
SESSION_DELETE = "session.delete"
FS_LIST = "fs.list"
FS_MKDIR = "fs.mkdir"
APPROVAL_RESPOND = "approval.respond"


class ClaudeExecutorAgent:
    def __init__(
        self,
        *,
        bridge: BridgeClient,
        machine_id: str,
        display_name: str,
        sync_interval_seconds: float = 30.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.bridge = bridge
        self.machine_id = machine_id
        self.display_name = display_name
        self.sync_interval_seconds = sync_interval_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._runs: dict[str, ClaudeRun] = {}      # session_id → ClaudeRun
        self._pending_runs: dict[str, ClaudeRun] = {}  # commandId → ClaudeRun (until session_id known)
        self._stop = False
        self._last_sync_at: float = 0.0

    # -------- main loop ---------------------------------------------------

    def run_forever(self) -> None:
        # First sync so the bridge sees us as online before we start
        # claiming commands.
        self._do_sync()
        while not self._stop:
            try:
                self._tick()
            except Exception:
                traceback.print_exc(file=sys.stderr)
            time.sleep(self.poll_interval_seconds)

    def stop(self) -> None:
        self._stop = True

    def _tick(self) -> None:
        now = time.time()
        if now - self._last_sync_at >= self.sync_interval_seconds:
            self._do_sync()
            self._last_sync_at = now
        try:
            command = self.bridge.claim_next()
        except RuntimeError as e:
            print(f"[claude-executor] claim failed: {e}", file=sys.stderr)
            return
        if not command:
            return
        self._dispatch(command)

    def _do_sync(self) -> None:
        try:
            sessions = scan_sessions()
            self.bridge.sync(
                machine={
                    "machineId": self.machine_id,
                    "displayName": self.display_name,
                    "source": "agent",
                    "capabilities": {"liveControl": True, "claudeRuntime": True},
                },
                sessions=sessions,
            )
            self._last_sync_at = time.time()
        except RuntimeError as e:
            print(f"[claude-executor] sync failed: {e}", file=sys.stderr)

    # -------- dispatcher --------------------------------------------------

    def _dispatch(self, command: dict[str, Any]) -> None:
        ctype = command.get("type")
        cid = command.get("commandId") or command.get("id")
        try:
            if ctype == RUN_START:
                self._handle_run_start(command)
            elif ctype == RUN_INPUT:
                self._handle_run_input(command)
            elif ctype == RUN_INTERRUPT:
                self._handle_run_interrupt(command)
            elif ctype == SESSION_DELETE:
                result = self._handle_session_delete(command)
                self.bridge.report_result(cid, status="completed", result=result)
            elif ctype == FS_LIST:
                result = self._handle_fs_list(command)
                self.bridge.report_result(cid, status="completed", result=result)
            elif ctype == FS_MKDIR:
                result = self._handle_fs_mkdir(command)
                self.bridge.report_result(cid, status="completed", result=result)
            elif ctype == APPROVAL_RESPOND:
                # Approval flow is delivered out-of-band via the PreToolUse
                # hook; the queue command form is a placeholder for the
                # broker, not yet acted on. Mark completed so the queue
                # doesn't stall.
                self.bridge.report_result(cid, status="completed", result={"acknowledged": True})
            else:
                self.bridge.report_result(
                    cid, status="failed",
                    error={"message": f"unsupported command type: {ctype}"},
                )
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            try:
                self.bridge.report_result(cid, status="failed", error={"message": str(e)})
            except Exception:
                pass

    # -------- handlers ----------------------------------------------------

    def _handle_run_start(self, command: dict[str, Any]) -> None:
        cid = command.get("commandId") or command.get("id")
        params = self._parse_payload(command)
        cwd = str(params.get("cwd") or "")
        prompt = str(params.get("prompt") or "")
        attachments = params.get("attachments") if isinstance(params.get("attachments"), list) else []
        model = params.get("model")
        permission_mode = params.get("permissionMode")
        if not cwd:
            self.bridge.report_result(cid, status="failed", error={"message": "missing_cwd"})
            return

        run = ClaudeRun(
            cwd=cwd,
            permission_mode=permission_mode,
            model=model,
            on_event=lambda ev: self._forward_event(run, ev),
            on_exit=lambda code: self._handle_run_exit(run, code),
        )
        self._pending_runs[cid] = run
        try:
            run.spawn()
            run.write_user_message(prompt, attachments=attachments)
            # Acknowledge immediately; the actual session_id arrives via
            # the system:init event and we then index the run by it.
            self.bridge.report_result(
                cid, status="completed",
                result={"started": True, "commandId": cid},
            )
        except Exception as e:
            try: run.close()
            except Exception: pass
            self._pending_runs.pop(cid, None)
            self.bridge.report_result(cid, status="failed", error={"message": str(e)})

    def _handle_run_input(self, command: dict[str, Any]) -> None:
        cid = command.get("commandId") or command.get("id")
        session_id = str(command.get("sessionId") or "")
        params = self._parse_payload(command)
        text = str(params.get("text") or "")
        attachments = params.get("attachments") if isinstance(params.get("attachments"), list) else []
        run = self._runs.get(session_id)
        if run is None:
            # No live run for this session — try to start a fresh resume.
            # Resume requires the session jsonl to exist on this PC.
            if not find_session_jsonl(session_id):
                self.bridge.report_result(
                    cid, status="failed",
                    error={"message": f"no live run and no jsonl for session {session_id}"},
                )
                return
            run = ClaudeRun(
                cwd=str(command.get("cwd") or "."),
                on_event=lambda ev: self._forward_event(run, ev, fallback_session_id=session_id),
                on_exit=lambda code: self._handle_run_exit(run, code),
                extra_args=["--resume", session_id],
            )
            self._runs[session_id] = run
            run.spawn()
        try:
            run.write_user_message(text, attachments=attachments)
            self.bridge.report_result(cid, status="completed", result={"queued": True})
        except Exception as e:
            self.bridge.report_result(cid, status="failed", error={"message": str(e)})

    def _handle_run_interrupt(self, command: dict[str, Any]) -> None:
        cid = command.get("commandId") or command.get("id")
        session_id = str(command.get("sessionId") or "")
        run = self._runs.get(session_id)
        if run is None:
            self.bridge.report_result(cid, status="completed", result={"alreadyIdle": True})
            return
        run.interrupt()
        self.bridge.report_result(cid, status="completed", result={"interrupted": True})

    def _handle_session_delete(self, command: dict[str, Any]) -> dict[str, Any]:
        session_id = str(command.get("sessionId") or "")
        if not _is_session_id(session_id):
            raise RuntimeError("invalid_session_id")
        run = self._runs.pop(session_id, None)
        if run is not None:
            try: run.close()
            except Exception: pass
        path = find_session_jsonl(session_id)
        if path is None:
            return {"deleted": False, "reason": "not_found"}
        try:
            path.unlink()
            return {"deleted": True, "path": str(path)}
        except OSError as e:
            raise RuntimeError(f"unlink failed: {e}")

    def _handle_fs_list(self, command: dict[str, Any]) -> dict[str, Any]:
        params = self._parse_payload(command)
        target = str(params.get("path") or "")
        return _fs_list_impl(target)

    def _handle_fs_mkdir(self, command: dict[str, Any]) -> dict[str, Any]:
        params = self._parse_payload(command)
        parent = str(params.get("parent") or "")
        name = str(params.get("name") or "")
        return _fs_mkdir_impl(parent, name)

    # -------- helpers -----------------------------------------------------

    def _parse_payload(self, command: dict[str, Any]) -> dict[str, Any]:
        prompt = command.get("prompt")
        if not prompt:
            return {}
        try:
            obj = json.loads(prompt)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _forward_event(
        self,
        run: ClaudeRun,
        event: dict[str, Any],
        *,
        fallback_session_id: str | None = None,
    ) -> None:
        # Pin the run to its session_id once we see system:init, then move
        # the entry from _pending_runs → _runs.
        sess_id = run.session_id or fallback_session_id or ""
        if run.session_id and run.session_id not in self._runs:
            # Promote from pending → keyed-by-session.
            for cid, pending in list(self._pending_runs.items()):
                if pending is run:
                    self._pending_runs.pop(cid, None)
                    break
            self._runs[run.session_id] = run
        try:
            self.bridge.publish_event(session_id=sess_id, event=event)
        except RuntimeError as e:
            print(f"[claude-executor] forward event failed: {e}", file=sys.stderr)

    def _handle_run_exit(self, run: ClaudeRun, code: int | None) -> None:
        # Drop from registries so a follow-up run.input triggers a fresh
        # spawn instead of writing to a dead pipe.
        for sid, alive in list(self._runs.items()):
            if alive is run: self._runs.pop(sid, None)
        for cid, pending in list(self._pending_runs.items()):
            if pending is run: self._pending_runs.pop(cid, None)


# -------- pure helpers (testable in isolation) ----------------------------

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_INVALID_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def _is_session_id(value: str) -> bool:
    return bool(_UUID_RE.match(value or ""))


def _fs_list_impl(target: str) -> dict[str, Any]:
    """Mirror of claude-remote/src/fs-list.js — list directory children
    (dirs only, dotfiles filtered, sorted)."""
    target = (target or "").strip()
    entries: list[dict[str, Any]] = []
    parent: str | None = None
    if not target or target in ("/", "\\"):
        # Root: drives on Windows, "/" on POSIX.
        if sys.platform == "win32":
            for letter in "CDEFGHI":
                root = Path(f"{letter}:/")
                if root.exists():
                    entries.append({"name": f"{letter}:\\", "fullPath": f"{letter}:\\", "isDir": True})
            return {"path": "", "parent": None, "entries": entries}
        else:
            return _list_children(Path("/"))

    # Expand ~ and resolve.
    if target == "~" or target.startswith("~/") or target.startswith("~\\"):
        target = str(Path.home() / target[1:].lstrip("/\\"))
    path_obj = Path(target).resolve()
    if not path_obj.exists():
        raise FileNotFoundError(target)
    if not path_obj.is_dir():
        raise NotADirectoryError(target)
    return _list_children(path_obj)


def _list_children(path_obj: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(path_obj.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        raise
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                entries.append({"name": child.name, "fullPath": str(child), "isDir": True})
        except OSError:
            pass
    parent = str(path_obj.parent) if path_obj.parent != path_obj else None
    return {"path": str(path_obj), "parent": parent, "entries": entries}


def _fs_mkdir_impl(parent: str, name: str) -> dict[str, Any]:
    parent = (parent or "").strip()
    name = (name or "").strip()
    if not parent or not name:
        raise ValueError("parent and name required")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("invalid name (contains separator or .)")
    if _INVALID_NAME_CHARS.search(name):
        raise ValueError("invalid name (Windows-reserved char)")
    parent_path = Path(parent).resolve()
    if not parent_path.exists():
        raise FileNotFoundError(parent)
    if not parent_path.is_dir():
        raise NotADirectoryError(parent)
    target = parent_path / name
    if target.exists():
        raise FileExistsError(str(target))
    target.mkdir()
    return {"path": str(target), "parent": str(parent_path), "name": name}
