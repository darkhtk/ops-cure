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

# Maximum number of agent slots to scan for in env. Slot 1 uses bare names
# (BRIDGE_AGENT_HANDLE); slots 2..N use suffixed names (BRIDGE_AGENT_2_HANDLE).
_MAX_AGENT_SLOTS = 8


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


def _slot_env(slot: int, key: str) -> str:
    """Slot 1 uses BRIDGE_AGENT_<KEY>; slot >=2 uses BRIDGE_AGENT_<N>_<KEY>."""
    if slot <= 1:
        return f"BRIDGE_AGENT_{key}"
    return f"BRIDGE_AGENT_{slot}_{key}"


def _slot_get(slot: int, key: str, default: str = "") -> str:
    return os.environ.get(_slot_env(slot, key), default).strip()


def _build_brain_for_slot(
    slot: int,
    *,
    handle: str,
    remote_claude_service,
) -> tuple[AgentBrain | None, str | None]:
    """Build a brain for the given agent slot. Returns (brain, pc_machine_id)
    where pc_machine_id is non-None when the brain dispatches to a PC and
    that machine_id should be added to the shared reply watcher.
    """
    brain_kind = _slot_get(slot, "BRAIN", "pc-claude").lower() or "pc-claude"
    if brain_kind == "echo":
        return EchoBrain(), None
    if brain_kind == "pc-claude":
        if remote_claude_service is None:
            logger.warning(
                "agent slot %d: BRAIN=pc-claude but remote_claude_service not "
                "wired -- slot disabled", slot,
            )
            return None, None
        machine_id = _slot_get(slot, "PC_MACHINE_ID")
        cwd = _slot_get(slot, "PC_CWD")
        if not machine_id or not cwd:
            logger.warning(
                "agent slot %d (handle=%s): BRAIN=pc-claude requires "
                "PC_MACHINE_ID + PC_CWD; slot disabled", slot, handle,
            )
            return None, None
        model = _slot_get(slot, "MODEL") or None
        permission = _slot_get(slot, "PC_PERMISSION_MODE") or "acceptEdits"
        brain = PCClaudeBrain(
            remote_claude_service=remote_claude_service,
            machine_id=machine_id,
            cwd=cwd,
            actor_handle=handle,
            model=model,
            permission_mode=permission,
            reply_watcher=None,  # attached below once shared watcher exists
        )
        return brain, machine_id
    if brain_kind == "claude":
        api_key = _slot_get(slot, "ANTHROPIC_API_KEY") or os.environ.get(
            "BRIDGE_ANTHROPIC_API_KEY", ""
        ).strip()
        if not api_key:
            logger.warning(
                "agent slot %d: BRAIN=claude needs ANTHROPIC_API_KEY -- "
                "slot disabled. Use BRAIN=pc-claude for the PC-CLI path.",
                slot,
            )
            return None, None
        model = _slot_get(slot, "MODEL") or "claude-opus-4-7"
        system = _slot_get(slot, "SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT
        return ClaudeBrain(api_key=api_key, model=model, system_prompt=system), None
    logger.warning(
        "agent slot %d: unknown BRAIN=%s -- slot disabled", slot, brain_kind
    )
    return None, None


def build_default_agent_service(
    *,
    broker,
    chat_service,
    remote_claude_service=None,
) -> AgentService | None:
    """Returns AgentService with up to _MAX_AGENT_SLOTS runners + (when any
    pc-claude slot is configured) a single shared RemoteClaudeReplyWatcher
    that handles every PC machine id across slots.

    Slot 1 reads bare env names (``BRIDGE_AGENT_HANDLE`` etc.). Slots 2..N
    read suffixed names (``BRIDGE_AGENT_2_HANDLE``). Slot 1 is required to
    be configured for the service to start; additional slots are optional
    and silently skipped if their HANDLE is unset.

    Production path (slot 1): BRAIN=pc-claude — agent enqueues runs to a
    worker PC running claude_executor.

    Multi-agent path: configure additional slots with distinct HANDLE +
    PC_MACHINE_ID (one per worker PC).
    """
    enabled = os.environ.get("BRIDGE_AGENT_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    svc = AgentService()
    pc_machine_ids: list[str] = []
    pc_brains: list[PCClaudeBrain] = []

    for slot in range(1, _MAX_AGENT_SLOTS + 1):
        if slot == 1:
            handle = _slot_get(slot, "HANDLE") or "@bridge-agent"
        else:
            handle = _slot_get(slot, "HANDLE")
            if not handle:
                continue  # slot not configured
        brain, pc_machine_id = _build_brain_for_slot(
            slot, handle=handle, remote_claude_service=remote_claude_service
        )
        if brain is None:
            if slot == 1:
                # Slot 1 misconfigured -- bail out completely.
                return None
            continue
        if pc_machine_id and isinstance(brain, PCClaudeBrain):
            pc_machine_ids.append(pc_machine_id)
            pc_brains.append(brain)
        runner = AgentRunner(
            actor_handle=handle,
            brain=brain,
            broker=broker,
            chat_service=chat_service,
        )
        svc.add_runner(runner)
        logger.info(
            "agent slot %d configured: handle=%s brain=%s",
            slot, handle, type(brain).__name__,
        )

    if not svc._runners:
        return None

    # Single shared watcher across all PC-bound slots so machine spaces are
    # subscribed exactly once and the FIFO bind ordering is per-machine.
    if pc_machine_ids:
        from .reply_watcher import RemoteClaudeReplyWatcher
        watcher = RemoteClaudeReplyWatcher(
            broker=broker,
            chat_service=chat_service,
            machine_ids=list(dict.fromkeys(pc_machine_ids)),  # de-dup, preserve order
            remote_claude_state_service=getattr(
                remote_claude_service, "state_service", None
            ),
        )
        for brain in pc_brains:
            brain._reply_watcher = watcher
        svc.set_reply_watcher(watcher)
    return svc
