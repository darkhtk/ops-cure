from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Any, Protocol

from ...process_io import build_utf8_subprocess_env, text_subprocess_kwargs
from ..chat_participant.runtime import (
    AppServerThreadClient,
    CodexAppServerProcessClient,
    CodexCliRuntimeConfig,
    CodexCurrentThreadRuntimeConfig,
    TurnActivityEvidence,
)


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionTaskContext:
    task_id: str
    machine_id: str
    thread_id: str
    objective: str
    success_criteria: dict[str, Any] = field(default_factory=dict)
    priority: str = "normal"
    origin_surface: str = "browser"
    owner_actor_id: str | None = None


@dataclass(slots=True)
class ExecutionResult:
    summary: str
    activity: TurnActivityEvidence | None = None


class RemoteExecutorRuntime(Protocol):
    def execute_task(self, context: ExecutionTaskContext) -> ExecutionResult | None: ...


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


def _extract_agent_message_text(turn: dict[str, Any]) -> str:
    fragments: list[str] = []
    for item in turn.get("items") or []:
        if str(item.get("type") or "") != "agentMessage":
            continue
        text = _compact_text(item.get("text"))
        if text:
            fragments.append(text)
    return "\n\n".join(fragments).strip()


def _extract_command_text(item: dict[str, Any]) -> str:
    command = _compact_text(item.get("command"))
    if command:
        return command
    for action in item.get("commandActions") or []:
        action_command = _compact_text(action.get("command"))
        if action_command:
            return action_command
    return ""


def _classify_command(command: str) -> str:
    lowered = command.lower()
    test_markers = (
        "pytest",
        "python -m pytest",
        "unittest",
        "vitest",
        "jest",
        "npm test",
        "pnpm test",
        "yarn test",
        "go test",
        "cargo test",
        "gradlew test",
        "gradle test",
        "ctest",
    )
    write_markers = (
        "apply_patch",
        "set-content",
        "add-content",
        "out-file",
        "move-item",
        "copy-item",
        "remove-item",
        "new-item",
        "rename-item",
        "git add",
        "git commit",
        "git mv",
        "git rm",
        "mkdir ",
        "md ",
    )
    read_markers = (
        "get-content",
        "type ",
        "cat ",
        "select-string",
        "rg ",
        "rg.exe",
        "findstr",
        "git diff",
        "git show",
        "git log",
        "git status",
        "get-childitem",
        "dir ",
        "ls ",
    )
    if any(marker in lowered for marker in test_markers):
        return "test"
    if any(marker in lowered for marker in write_markers):
        return "write"
    if any(marker in lowered for marker in read_markers):
        return "read"
    return "other"


def _extract_turn_activity_evidence(turn: dict[str, Any] | None) -> TurnActivityEvidence:
    if not turn:
        return TurnActivityEvidence()

    item_types: list[str] = []
    command_execution_count = 0
    read_command_count = 0
    write_command_count = 0
    test_command_count = 0
    other_activity_count = 0

    for item in turn.get("items") or []:
        item_type = str(item.get("type") or "")
        if not item_type:
            continue
        item_types.append(item_type)
        if item_type == "commandExecution":
            command_execution_count += 1
            classification = _classify_command(_extract_command_text(item))
            if classification == "read":
                read_command_count += 1
            elif classification == "write":
                write_command_count += 1
            elif classification == "test":
                test_command_count += 1
            else:
                other_activity_count += 1
            continue
        if item_type not in {"userMessage", "agentMessage"}:
            other_activity_count += 1

    return TurnActivityEvidence(
        item_types=tuple(dict.fromkeys(item_types)),
        command_execution_count=command_execution_count,
        read_command_count=read_command_count,
        write_command_count=write_command_count,
        test_command_count=test_command_count,
        other_activity_count=other_activity_count,
    )


def _build_prompt(context: ExecutionTaskContext) -> str:
    success_criteria = json.dumps(context.success_criteria, ensure_ascii=False, indent=2) if context.success_criteria else "{}"
    return (
        "You are a local Codex execution worker attached to an Opscure remote task.\n"
        "Do the smallest real local work needed to advance or complete the task before replying.\n"
        "Prefer concrete reading, editing, and testing over meta planning.\n"
        "If the task cannot be completed because approval or information is missing, say exactly what is blocked.\n"
        "Return only a concise execution summary for the bridge operator. Do not include markdown fences.\n\n"
        "Task context:\n"
        f"- task_id: {context.task_id}\n"
        f"- machine_id: {context.machine_id}\n"
        f"- thread_id: {context.thread_id}\n"
        f"- origin_surface: {context.origin_surface}\n"
        f"- priority: {context.priority}\n"
        f"- owner_actor_id: {context.owner_actor_id or '(unassigned)'}\n\n"
        "Objective:\n"
        f"{context.objective}\n\n"
        "Success criteria (JSON):\n"
        f"{success_criteria}\n"
    )


def _runtime_env_var(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


class CodexCliRemoteExecutorRuntime:
    def __init__(
        self,
        *,
        config: CodexCliRuntimeConfig,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.config = config
        self.command_runner = command_runner or subprocess.run

    @classmethod
    def from_env(cls, *, cwd: str | None = None) -> "CodexCliRemoteExecutorRuntime":
        executable = _runtime_env_var(
            "REMOTE_EXECUTOR_CODEX_EXECUTABLE",
            "CHAT_PARTICIPANT_CODEX_EXECUTABLE",
            "CODEX_EXECUTABLE",
            default="codex",
        ) or "codex"
        config = CodexCliRuntimeConfig(
            executable=_resolve_executable(executable),
            extra_args=[],
            cwd=cwd,
            model=_runtime_env_var("REMOTE_EXECUTOR_CODEX_MODEL", "CHAT_PARTICIPANT_CODEX_MODEL"),
            profile=_runtime_env_var("REMOTE_EXECUTOR_CODEX_PROFILE", "CHAT_PARTICIPANT_CODEX_PROFILE"),
            sandbox_mode=_runtime_env_var("REMOTE_EXECUTOR_CODEX_SANDBOX", "CHAT_PARTICIPANT_CODEX_SANDBOX", default="workspace-write") or "workspace-write",
            skip_git_repo_check=(_runtime_env_var("REMOTE_EXECUTOR_CODEX_SKIP_GIT_CHECK", "CHAT_PARTICIPANT_CODEX_SKIP_GIT_CHECK", default="true") or "true").lower() != "false",
            add_dirs=[],
        )
        return cls(config=config)

    def execute_task(self, context: ExecutionTaskContext) -> ExecutionResult | None:
        prompt = _build_prompt(context)
        runtime_cwd = str(Path(self.config.cwd or os.getcwd()).resolve())
        env = build_utf8_subprocess_env()

        with tempfile.TemporaryDirectory(prefix="remote-executor-codex-") as temp_dir:
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
                    f"Codex remote-executor runtime failed with exit code {completed.returncode}: {detail}",
                )

            summary = ""
            if output_file.exists():
                summary = output_file.read_text(encoding="utf-8").strip()
            if not summary:
                summary = _compact_text(completed.stdout)
            if not summary:
                return None
            return ExecutionResult(
                summary=summary,
                activity=TurnActivityEvidence(
                    item_types=("cliExecution",),
                    command_execution_count=1,
                ),
            )

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
        command.extend(self.config.extra_args)
        command.append("-")
        return command


class CodexCurrentThreadRemoteExecutorRuntime:
    def __init__(
        self,
        *,
        config: CodexCurrentThreadRuntimeConfig,
        client: AppServerThreadClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or CodexAppServerProcessClient(config=config)

    @classmethod
    def from_env(
        cls,
        *,
        cwd: str | None = None,
        thread_id: str | None = None,
    ) -> "CodexCurrentThreadRemoteExecutorRuntime":
        executable = _runtime_env_var(
            "REMOTE_EXECUTOR_CODEX_EXECUTABLE",
            "CHAT_PARTICIPANT_CODEX_EXECUTABLE",
            "CODEX_EXECUTABLE",
            default="codex",
        ) or "codex"
        config = CodexCurrentThreadRuntimeConfig(
            executable=_resolve_executable(executable),
            runtime_args=["app-server"],
            cwd=cwd,
            thread_id=thread_id
            or _runtime_env_var(
                "REMOTE_EXECUTOR_CODEX_THREAD_ID",
                "CHAT_PARTICIPANT_CODEX_THREAD_ID",
                "CODEX_THREAD_ID",
            ),
        )
        return cls(config=config)

    def execute_task(self, context: ExecutionTaskContext) -> ExecutionResult | None:
        if not self.config.thread_id:
            raise RuntimeError("Current-thread remote-executor runtime requires a Codex thread id.")

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
        matched_turn = completed_turn if str(completed_turn.get("id") or "") == turn_id else None
        status = str(completed_turn.get("status") or "")
        if status == "failed":
            error = completed_turn.get("error") or {}
            detail = error.get("message") or error or "unknown turn error"
            raise RuntimeError(f"Current-thread remote-executor turn failed: {detail}")
        if status == "interrupted":
            raise RuntimeError("Current-thread remote-executor turn was interrupted before completion.")

        summary = streamed_reply.strip()
        if matched_turn is None or not (matched_turn.get("items") or []):
            thread = self.client.read_thread(thread_id, include_turns=True).get("thread") or {}
            matched_turn = next(
                (item for item in thread.get("turns") or [] if str(item.get("id") or "") == turn_id),
                None,
            )
        activity = _extract_turn_activity_evidence(matched_turn)
        if not summary and matched_turn is not None:
            summary = _extract_agent_message_text(matched_turn)
        if not summary:
            return None
        return ExecutionResult(summary=summary, activity=activity)
