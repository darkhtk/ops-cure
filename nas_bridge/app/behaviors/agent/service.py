"""AgentService — bridge-lifespan orchestration of one or more runners."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from .brains import (
    AgentBrain, ClaudeBrain, EchoBrain, PCClaudeBrain, DEFAULT_SYSTEM_PROMPT,
)
from .runner import AgentRunner

logger = logging.getLogger("opscure.agent.service")


class AgentService:
    """Owns AgentRunners + an optional RemoteClaudeReplyWatcher and
    starts/stops their async tasks alongside the FastAPI lifespan."""

    def __init__(self) -> None:
        self._runners: list[AgentRunner] = []
        self._tasks: list[asyncio.Task] = []
        self._reply_watcher = None

    def add_runner(self, runner: AgentRunner) -> None:
        self._runners.append(runner)

    def set_reply_watcher(self, watcher) -> None:
        self._reply_watcher = watcher

    async def start(self) -> None:
        if self._reply_watcher is not None:
            await self._reply_watcher.start()
        for runner in self._runners:
            task = asyncio.create_task(
                runner.run_forever(),
                name=f"agent-runner:{runner.actor_handle}",
            )
            self._tasks.append(task)
            logger.info(
                "agent runner started: handle=%s tasks=%d",
                runner.actor_handle, len(self._tasks),
            )

    async def stop(self) -> None:
        for runner in self._runners:
            runner.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._reply_watcher is not None:
            await self._reply_watcher.stop()


def build_default_agent_service(
    *,
    broker,
    chat_service,
    remote_claude_service=None,
) -> AgentService | None:
    """Returns AgentService with runner + (for pc-claude brain) reply
    watcher already wired. Caller awaits svc.start() in lifespan.
    """
    """Reads BRIDGE_AGENT_* env to decide whether to spawn an agent
    in this process. Returns ``None`` when disabled (the common dev
    case).

    Production path: BRIDGE_AGENT_BRAIN=pc-claude. The agent enqueues
    runs to a worker PC running claude_executor (which uses the user's
    locally-logged-in Claude session -- no API key on the bridge).

    Test paths:
      BRIDGE_AGENT_BRAIN=echo           deterministic stub
      BRIDGE_AGENT_BRAIN=claude         direct anthropic SDK; needs
                                        anthropic installed + API key.
                                        NOT bundled in default image.
    """
    enabled = os.environ.get("BRIDGE_AGENT_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    handle = os.environ.get("BRIDGE_AGENT_HANDLE", "@bridge-agent").strip() or "@bridge-agent"
    brain_kind = os.environ.get("BRIDGE_AGENT_BRAIN", "pc-claude").strip().lower()
    brain: AgentBrain

    if brain_kind == "echo":
        brain = EchoBrain()
    elif brain_kind == "pc-claude":
        if remote_claude_service is None:
            logger.warning(
                "BRIDGE_AGENT_BRAIN=pc-claude but remote_claude_service not "
                "wired -- agent disabled"
            )
            return None
        machine_id = os.environ.get("BRIDGE_AGENT_PC_MACHINE_ID", "").strip()
        cwd = os.environ.get("BRIDGE_AGENT_PC_CWD", "").strip()
        if not machine_id or not cwd:
            logger.warning(
                "BRIDGE_AGENT_BRAIN=pc-claude requires "
                "BRIDGE_AGENT_PC_MACHINE_ID + BRIDGE_AGENT_PC_CWD"
            )
            return None
        model = os.environ.get("BRIDGE_AGENT_MODEL", "").strip() or None
        permission = os.environ.get(
            "BRIDGE_AGENT_PC_PERMISSION_MODE", "acceptEdits"
        ).strip()
        from .reply_watcher import RemoteClaudeReplyWatcher

        watcher = RemoteClaudeReplyWatcher(
            broker=broker,
            chat_service=chat_service,
            machine_ids=[machine_id],
        )
        brain = PCClaudeBrain(
            remote_claude_service=remote_claude_service,
            machine_id=machine_id,
            cwd=cwd,
            actor_handle=handle,
            model=model,
            permission_mode=permission,
            reply_watcher=watcher,
        )
    elif brain_kind == "claude":
        api_key = os.environ.get("BRIDGE_ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.warning(
                "BRIDGE_AGENT_BRAIN=claude (api-key path) but "
                "BRIDGE_ANTHROPIC_API_KEY not set -- agent disabled. "
                "Use BRIDGE_AGENT_BRAIN=pc-claude for the PC-CLI path."
            )
            return None
        model = os.environ.get("BRIDGE_AGENT_MODEL", "claude-opus-4-7").strip()
        system = os.environ.get("BRIDGE_AGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
        brain = ClaudeBrain(api_key=api_key, model=model, system_prompt=system)
    else:
        logger.warning("unknown BRIDGE_AGENT_BRAIN=%s -- agent disabled", brain_kind)
        return None

    runner = AgentRunner(
        actor_handle=handle,
        brain=brain,
        broker=broker,
        chat_service=chat_service,
    )
    svc = AgentService()
    svc.add_runner(runner)
    # If pc-claude path created a watcher, attach it -- AgentService.start()
    # will spawn its tasks.
    if brain_kind == "pc-claude":
        watcher = getattr(brain, "_reply_watcher", None)
        if watcher is not None:
            svc.set_reply_watcher(watcher)
    return svc
