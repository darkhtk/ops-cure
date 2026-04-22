from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Any, Protocol
from uuid import uuid4

from ...process_io import build_utf8_subprocess_env, text_subprocess_kwargs


LOGGER = logging.getLogger(__name__)
DEFAULT_APP_SERVER_ARGS = ["app-server"]
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_TURN_TIMEOUT_SECONDS = 180.0
DEFAULT_CLIENT_INFO = {
    "name": "opscure_chat_participant",
    "title": "Opscure Chat Participant",
    "version": "0.1.0",
}


@dataclass(slots=True)
class ReplyContext:
    actor_name: str
    actor_kind: str
    thread_id: str
    space_id: str
    room_title: str
    room_topic: str | None
    machine_label: str | None
    participants: list[dict[str, Any]] = field(default_factory=list)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ReplyResult:
    content: str


class ChatParticipantRuntime(Protocol):
    def generate_reply(self, context: ReplyContext) -> ReplyResult | None: ...


class AppServerThreadClient(Protocol):
    def resume_thread(self, thread_id: str) -> dict[str, Any]: ...

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]: ...

    def start_turn(self, thread_id: str, prompt: str) -> dict[str, Any]: ...

    def wait_for_turn_completion(
        self,
        *,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], str]: ...

    def close(self) -> None: ...


def _load_json_list_env(*names: str) -> list[str]:
    for name in names:
        raw = os.getenv(name)
        if not raw:
            continue
        value = json.loads(raw)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RuntimeError(f"{name} must be a JSON array of strings.")
        return list(value)
    return []


def _load_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - env parse guard
        raise RuntimeError(f"{name} must be numeric.") from exc


def _compact_text(value: Any) -> str:
    text = str(value or "").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    compacted = "\n".join(line for line in lines if line)
    return compacted.strip()


def _resolve_executable(executable: str) -> str:
    candidate = executable.strip()
    if not candidate:
        return executable
    if Path(candidate).suffix:
        return candidate
    cmd_candidate = which(f"{candidate}.cmd")
    if cmd_candidate:
        return cmd_candidate
    exe_candidate = which(f"{candidate}.exe")
    if exe_candidate:
        return exe_candidate
    direct_candidate = which(candidate)
    return direct_candidate or candidate


def _normalize_app_server_args(runtime_args: list[str]) -> list[str]:
    normalized = [str(value).strip() for value in runtime_args if str(value).strip()]
    if not normalized:
        return list(DEFAULT_APP_SERVER_ARGS)
    if normalized[0] != "app-server":
        return [*DEFAULT_APP_SERVER_ARGS, *normalized]
    return normalized


def _build_prompt(context: ReplyContext) -> str:
    participant_lines = []
    for participant in context.participants:
        name = participant.get("actor_name") or participant.get("name") or "unknown"
        kind = participant.get("actor_kind") or participant.get("kind") or "participant"
        participant_lines.append(f"- {name} [{kind}]")
    participants_text = "\n".join(participant_lines) if participant_lines else "- none"

    recent_lines = []
    for message in context.recent_messages:
        actor_name = message.get("actor_name") or "unknown"
        content = _compact_text(message.get("content") or "")
        recent_lines.append(f"[{actor_name}] {content}")
    recent_messages_text = "\n".join(recent_lines) if recent_lines else "(no recent messages)"

    machine_label = context.machine_label or "(unspecified)"
    room_topic = context.room_topic or "(none)"
    return (
        "You are a local Codex participant attached to an Opscure chat room.\n"
        f"Your participant name is `{context.actor_name}` and your kind is `{context.actor_kind}`.\n"
        "Return only the single reply message body that should be posted back into the room.\n"
        "Do not include role labels, markdown fences, or extra explanations outside the reply.\n"
        "Treat this as a conversational relay. Do not use tools, inspect files, or perform actions.\n"
        "Keep the reply concise, collaborative, and directly responsive to the room context.\n\n"
        "Room context:\n"
        f"- space_id: {context.space_id}\n"
        f"- thread_id: {context.thread_id}\n"
        f"- title: {context.room_title}\n"
        f"- topic: {room_topic}\n"
        f"- machine_label: {machine_label}\n\n"
        "Participants:\n"
        f"{participants_text}\n\n"
        "Recent messages (oldest to newest):\n"
        f"{recent_messages_text}\n"
    )


def _extract_agent_message_text(turn: dict[str, Any]) -> str:
    fragments: list[str] = []
    for item in turn.get("items") or []:
        if str(item.get("type") or "") != "agentMessage":
            continue
        text = _compact_text(item.get("text"))
        if text:
            fragments.append(text)
    return "\n\n".join(fragments).strip()


@dataclass(slots=True)
class CodexCliRuntimeConfig:
    executable: str = "codex"
    extra_args: list[str] = field(default_factory=list)
    cwd: str | None = None
    model: str | None = None
    profile: str | None = None
    sandbox_mode: str = "read-only"
    skip_git_repo_check: bool = True
    add_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, *, cwd: str | None = None) -> "CodexCliRuntimeConfig":
        executable = (
            os.getenv("CHAT_PARTICIPANT_CODEX_EXECUTABLE")
            or os.getenv("CODEX_EXECUTABLE")
            or "codex"
        )
        extra_args = _load_json_list_env(
            "CHAT_PARTICIPANT_CODEX_ARGS_JSON",
            "CODEX_ARGS_JSON",
        )
        add_dirs = _load_json_list_env("CHAT_PARTICIPANT_CODEX_ADD_DIRS_JSON")
        return cls(
            executable=_resolve_executable(executable),
            extra_args=extra_args,
            cwd=cwd,
            model=os.getenv("CHAT_PARTICIPANT_CODEX_MODEL") or None,
            profile=os.getenv("CHAT_PARTICIPANT_CODEX_PROFILE") or None,
            sandbox_mode=os.getenv("CHAT_PARTICIPANT_CODEX_SANDBOX") or "read-only",
            skip_git_repo_check=(os.getenv("CHAT_PARTICIPANT_CODEX_SKIP_GIT_CHECK", "true").lower() != "false"),
            add_dirs=add_dirs,
        )


@dataclass(slots=True)
class CodexCurrentThreadRuntimeConfig:
    executable: str = "codex"
    runtime_args: list[str] = field(default_factory=list)
    cwd: str | None = None
    thread_id: str | None = None
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS

    @classmethod
    def from_env(
        cls,
        *,
        cwd: str | None = None,
        thread_id: str | None = None,
    ) -> "CodexCurrentThreadRuntimeConfig":
        executable = (
            os.getenv("CHAT_PARTICIPANT_CODEX_EXECUTABLE")
            or os.getenv("CODEX_EXECUTABLE")
            or "codex"
        )
        runtime_args = _load_json_list_env(
            "CHAT_PARTICIPANT_CODEX_APP_SERVER_ARGS_JSON",
            "CODEX_APP_SERVER_ARGS_JSON",
        )
        return cls(
            executable=_resolve_executable(executable),
            runtime_args=_normalize_app_server_args(runtime_args),
            cwd=cwd,
            thread_id=thread_id or os.getenv("CHAT_PARTICIPANT_CODEX_THREAD_ID") or os.getenv("CODEX_THREAD_ID"),
            request_timeout_seconds=_load_float_env(
                "CHAT_PARTICIPANT_CODEX_REQUEST_TIMEOUT_SECONDS",
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
            ),
            turn_timeout_seconds=_load_float_env(
                "CHAT_PARTICIPANT_CODEX_TURN_TIMEOUT_SECONDS",
                DEFAULT_TURN_TIMEOUT_SECONDS,
            ),
        )


class CodexCliChatParticipantRuntime:
    def __init__(
        self,
        *,
        config: CodexCliRuntimeConfig,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.config = config
        self.command_runner = command_runner or subprocess.run

    def generate_reply(self, context: ReplyContext) -> ReplyResult | None:
        prompt = _build_prompt(context)
        runtime_cwd = str(Path(self.config.cwd or os.getcwd()).resolve())
        env = build_utf8_subprocess_env()

        with tempfile.TemporaryDirectory(prefix="chat-participant-codex-") as temp_dir:
            output_file = Path(temp_dir) / "last_message.txt"
            command = self._build_command(output_file=output_file, cwd=runtime_cwd)
            completed = self.command_runner(
                command,
                input=prompt,
                capture_output=True,
                cwd=runtime_cwd,
                env=env,
                **text_subprocess_kwargs(),
            )

            if completed.returncode != 0:
                stdout = _compact_text(completed.stdout)
                stderr = _compact_text(completed.stderr)
                detail_parts = [part for part in [stdout, stderr] if part]
                detail = "\n\n".join(detail_parts) if detail_parts else "no output captured"
                raise RuntimeError(
                    f"Codex chat participant runtime failed with exit code {completed.returncode}: {detail}",
                )

            content = ""
            if output_file.exists():
                content = output_file.read_text(encoding="utf-8").strip()
            if not content:
                content = _compact_text(completed.stdout)
            if not content:
                return None
            return ReplyResult(content=content)

    def _build_command(self, *, output_file: Path, cwd: str) -> list[str]:
        command = [
            _resolve_executable(self.config.executable),
            "exec",
            "--color",
            "never",
            "--output-last-message",
            str(output_file),
            "-C",
            cwd,
            "-s",
            self.config.sandbox_mode,
        ]
        if self.config.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if self.config.model:
            command.extend(["-m", self.config.model])
        if self.config.profile:
            command.extend(["-p", self.config.profile])
        for add_dir in self.config.add_dirs:
            command.extend(["--add-dir", add_dir])
        command.extend(self.config.extra_args)
        command.append("-")
        return command


class CodexAppServerProcessClient:
    def __init__(self, *, config: CodexCurrentThreadRuntimeConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._initialized = False

    def close(self) -> None:
        process = self._process
        self._process = None
        self._initialized = False
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        return self._send_request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": bool(include_turns),
            },
        )

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return self._send_request("thread/resume", {"threadId": thread_id})

    def start_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        return self._send_request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            },
        )

    def wait_for_turn_completion(
        self,
        *,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], str]:
        deadline = time.monotonic() + timeout_seconds
        fragments: list[str] = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stderr_tail = " | ".join(self._stderr_tail)
                detail = f" stderr={stderr_tail}" if stderr_tail else ""
                raise TimeoutError(f"Timed out waiting for turn {turn_id} to complete.{detail}")

            try:
                message = self._notifications.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue

            method = str(message.get("method") or "")
            params = message.get("params") or {}
            if str(params.get("threadId") or "") != thread_id:
                continue
            current_turn_id = str(params.get("turnId") or params.get("turn", {}).get("id") or "")
            if current_turn_id != turn_id:
                continue

            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    fragments.append(delta)
                continue

            if method == "turn/completed":
                turn = params.get("turn") or {}
                return turn, "".join(fragments).strip()

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._ensure_started()
        return self._send_request_no_start(method, params)

    def _send_request_no_start(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = f"{method}:{uuid4().hex}"
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

        with self._pending_lock:
            self._pending[request_id] = response_queue

        payload = {
            "id": request_id,
            "method": method,
            "params": params,
        }

        try:
            with self._write_lock:
                assert self._process is not None and self._process.stdin is not None
                self._process.stdin.write(f"{json.dumps(payload)}\n")
                self._process.stdin.flush()
            response = response_queue.get(timeout=self.config.request_timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError(f"{method} timed out after {self.config.request_timeout_seconds:.1f}s.") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        error = response.get("error")
        if error:
            detail = error.get("message") or error
            raise RuntimeError(f"{method} failed: {detail}")
        result = response.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(response, dict):
            return response
        raise RuntimeError(f"{method} returned an unexpected response payload.")

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            if self._initialized:
                return
        else:
            self.close()

        runtime_cwd = str(Path(self.config.cwd or os.getcwd()).resolve())
        command = [_resolve_executable(self.config.executable), *self.config.runtime_args]
        env = build_utf8_subprocess_env()
        self._process = subprocess.Popen(
            command,
            cwd=runtime_cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            **text_subprocess_kwargs(),
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._send_request_no_start("initialize", {"clientInfo": dict(DEFAULT_CLIENT_INFO)})
        self._initialized = True

    def _read_stdout_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Ignoring non-JSON app-server stdout: %s", line)
                continue
            request_id = str(message.get("id") or "")
            if request_id:
                with self._pending_lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    pending.put(message)
                    continue
            if isinstance(message, dict):
                self._notifications.put(message)

    def _read_stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw_line in process.stderr:
            line = _compact_text(raw_line)
            if not line:
                continue
            self._stderr_tail.append(line)
            LOGGER.warning("Codex app-server stderr: %s", line)


class CodexCurrentThreadChatParticipantRuntime:
    def __init__(
        self,
        *,
        config: CodexCurrentThreadRuntimeConfig,
        client: AppServerThreadClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or CodexAppServerProcessClient(config=config)

    def generate_reply(self, context: ReplyContext) -> ReplyResult | None:
        if not self.config.thread_id:
            raise RuntimeError(
                "Current-thread runtime requires CHAT_PARTICIPANT_CODEX_THREAD_ID or CODEX_THREAD_ID.",
            )

        prompt = _build_prompt(context)
        thread_id = self.config.thread_id
        self.client.resume_thread(thread_id)
        started = self.client.start_turn(thread_id, prompt)
        turn = started.get("turn") or {}
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise RuntimeError("turn/start did not return a turn id.")

        completed_turn, streamed_reply = self.client.wait_for_turn_completion(
            thread_id=thread_id,
            turn_id=turn_id,
            timeout_seconds=self.config.turn_timeout_seconds,
        )
        status = str(completed_turn.get("status") or "")
        if status == "failed":
            error = completed_turn.get("error") or {}
            detail = error.get("message") or error or "unknown turn error"
            raise RuntimeError(f"Current-thread turn failed: {detail}")
        if status == "interrupted":
            raise RuntimeError("Current-thread turn was interrupted before completion.")

        content = streamed_reply.strip()
        if not content:
            thread = self.client.read_thread(thread_id, include_turns=True).get("thread") or {}
            matched_turn = next(
                (item for item in thread.get("turns") or [] if str(item.get("id") or "") == turn_id),
                None,
            )
            if matched_turn is not None:
                content = _extract_agent_message_text(matched_turn)
        if not content:
            return None
        return ReplyResult(content=content)
