from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class AdapterContext:
    session_id: str
    project_name: str
    agent_name: str
    job_type: str
    system_prompt: str
    user_text: str
    open_tools: list[str]
    preset: str | None
    session_status: str
    session_summary: str | None
    project_workdir: str
    session_workspace: str
    session_workspace_relative: str
    available_agents: list[dict[str, Any]]
    recent_transcript: list[dict[str, Any]]


class BaseCliAdapter:
    cli_name: str = ""
    executable_env: str = ""
    args_env: str = ""
    default_executable: str = ""

    def build_command(self) -> list[str]:
        executable = os.getenv(self.executable_env, self.default_executable)
        args = self._load_args_env(self.args_env)
        return [executable, *args]

    def prepare_input(self, context: AdapterContext) -> str:
        tools = ", ".join(context.open_tools) if context.open_tools else "none"
        preset = context.preset or "(unknown)"
        session_summary = context.session_summary or "- none"
        agent_lines = []
        for agent in context.available_agents:
            default_marker = " default" if agent.get("is_default") else ""
            agent_lines.append(
                f"- {agent.get('agent_name')} [{agent.get('cli_type')}] "
                f"{agent.get('role')} status={agent.get('status')}{default_marker}"
            )
        transcript_lines = []
        for entry in context.recent_transcript:
            transcript_lines.append(
                f"- [{entry.get('direction')}] {entry.get('actor')}: {entry.get('content')}",
            )
        available_agents = "\n".join(agent_lines) if agent_lines else "- none"
        recent_transcript = "\n".join(transcript_lines) if transcript_lines else "- none"
        return (
            f"System prompt:\n{context.system_prompt}\n\n"
            "Shared session context:\n"
            f"- Session ID: {context.session_id}\n"
            f"- Session name: {context.project_name}\n"
            f"- Preset: {preset}\n"
            f"- Session status: {context.session_status}\n"
            f"- Current agent: {context.agent_name}\n"
            f"- Current job type: {context.job_type}\n"
            f"- Project workdir: {context.project_workdir}\n"
            f"- Session workspace: {context.session_workspace}\n"
            f"Preferred open tools: {tools}\n\n"
            "Team roster:\n"
            f"{available_agents}\n\n"
            "Collaboration protocol:\n"
            "- You are one agent in a shared thread with the other listed agents.\n"
            "- Use the local session workspace as the source of truth for long-form notes.\n"
            f"- Store detailed artifacts under `{context.session_workspace_relative}` instead of dumping them into Discord.\n"
            "- Read CURRENT_STATE.md first, then consult TASK_BOARD.md, TASKS/*.md, STATUS.md, HANDOFFS.md, REPORT.md, and AGENTS/<agent>.md as needed.\n"
            "- Update local markdown files such as CURRENT_STATE.md, TASK_BOARD.md, TASKS/*.md, STATUS.md, REPORT.md, CRITICAL_QUESTIONS.md, HANDOFFS.md, and AGENTS/<agent>.md when appropriate.\n"
            "- Discord output must stay short and control-plane only: work directives, short reports, critical blocking questions, and control commands.\n"
            "- You can ask another agent to continue by appending a handoff block.\n"
            "- Use this exact format when a handoff is needed:\n"
            "[[handoff agent=\"coder\"]]\n"
            "T-002\n"
            "Target summary: One focused next action.\n"
            "Read CURRENT_STATE.md and TASK_BOARD.md first.\n"
            "Files: src/example.py\n"
            "Done condition: concrete finish state.\n"
            "[[/handoff]]\n"
            "- Every handoff body must include a `T-###` task id, a `Target summary:` line, and the `Read CURRENT_STATE.md and TASK_BOARD.md first.` reminder or the bridge will reject it.\n"
            "- Each handoff should represent one focused task-card-sized next action. Keep file scopes disjoint when queuing multiple tasks in parallel.\n"
            "- Keep the handoff block compact: one focused next action plus file references. Put long detail into TASKS/*.md and HANDOFFS.md instead.\n"
            "- Use this exact format for the operator-facing report:\n"
            "[[report]]\n"
            "Short report for Discord.\n"
            "[[/report]]\n"
            "- Use this exact format only for critical blocking questions:\n"
            "[[question]]\n"
            "Question that truly needs the operator.\n"
            "[[/question]]\n"
            "- Keep handoffs concrete, brief, and self-contained. Prefer under 6 bullet points and under 800 characters in stdout.\n"
            "- Do not print long plans, long reviews, or long implementation logs to stdout. Put them in markdown files instead.\n\n"
            "Session memory:\n"
            f"{session_summary}\n\n"
            "Recent session transcript:\n"
            f"{recent_transcript}\n\n"
            "Current message to handle:\n"
            f"{context.user_text}\n"
        )

    def combine_output(self, stdout: str, stderr: str, return_code: int) -> str:
        stdout = stdout.strip()
        stderr = stderr.strip()
        if return_code == 0:
            return stdout or "(command completed with no stdout)"
        combined = stdout
        if stderr:
            combined = f"{combined}\n\nstderr:\n{stderr}".strip()
        return combined or f"CLI exited with code {return_code}"

    @staticmethod
    def _load_args_env(name: str) -> list[str]:
        raw = os.getenv(name, "[]")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{name} must be valid JSON array text.") from exc
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RuntimeError(f"{name} must be a JSON array of strings.")
        return value


class CodexCliAdapter(BaseCliAdapter):
    cli_name = "codex"
    executable_env = "CODEX_EXECUTABLE"
    args_env = "CODEX_ARGS_JSON"
    default_executable = "codex"


class ClaudeCliAdapter(BaseCliAdapter):
    cli_name = "claude"
    executable_env = "CLAUDE_EXECUTABLE"
    args_env = "CLAUDE_ARGS_JSON"
    default_executable = "claude"


class MockCliAdapter(BaseCliAdapter):
    cli_name = "mock"
    executable_env = "MOCK_EXECUTABLE"
    args_env = "MOCK_ARGS_JSON"
    default_executable = sys.executable

    def build_command(self) -> list[str]:
        return [
            sys.executable,
            "-c",
            (
                "import sys; "
                "data = sys.stdin.read(); "
                "print('MOCK RESPONSE\\n' + data[:4000])"
            ),
        ]


ADAPTERS: dict[str, type[BaseCliAdapter]] = {
    "codex": CodexCliAdapter,
    "claude": ClaudeCliAdapter,
    "mock": MockCliAdapter,
}


def get_adapter(cli_name: str) -> BaseCliAdapter:
    adapter_cls = ADAPTERS.get(cli_name)
    if adapter_cls is None:
        raise ValueError(f"Unsupported CLI adapter '{cli_name}'.")
    return adapter_cls()
