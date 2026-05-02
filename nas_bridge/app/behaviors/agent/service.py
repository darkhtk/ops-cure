"""AgentService — bridge-lifespan orchestration of one or more runners."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from .brains import AgentBrain, ClaudeBrain, EchoBrain, DEFAULT_SYSTEM_PROMPT
from .runner import AgentRunner

logger = logging.getLogger("opscure.agent.service")


class AgentService:
    """Owns a list of AgentRunners and starts/stops their async tasks
    alongside the FastAPI lifespan."""

    def __init__(self) -> None:
        self._runners: list[AgentRunner] = []
        self._tasks: list[asyncio.Task] = []

    def add_runner(self, runner: AgentRunner) -> None:
        self._runners.append(runner)

    async def start(self) -> None:
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


def build_default_agent_service(
    *,
    broker,
    chat_service,
) -> AgentService | None:
    """Reads BRIDGE_AGENT_* env to decide whether to spawn an agent
    in this process. Returns ``None`` when disabled (the common dev
    case). Production / live deployments set BRIDGE_AGENT_ENABLED=true
    + an API key.
    """
    enabled = os.environ.get("BRIDGE_AGENT_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    handle = os.environ.get("BRIDGE_AGENT_HANDLE", "@bridge-agent").strip() or "@bridge-agent"
    brain_kind = os.environ.get("BRIDGE_AGENT_BRAIN", "claude").strip().lower()
    brain: AgentBrain
    if brain_kind == "echo":
        brain = EchoBrain()
    elif brain_kind == "claude":
        api_key = os.environ.get("BRIDGE_ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.warning(
                "BRIDGE_AGENT_ENABLED=true with brain=claude but "
                "BRIDGE_ANTHROPIC_API_KEY not set -- agent disabled"
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
    return svc
