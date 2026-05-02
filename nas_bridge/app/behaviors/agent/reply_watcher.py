"""RemoteClaudeReplyWatcher — translate PC claude run output back into
the originating v2 op as a speech.claim.

Flow:
  1. PCClaudeBrain.respond enqueues a remote_claude run; it ALSO calls
     watcher.register_dispatch(command_id, op context) so the watcher
     can match later events back to the op.
  2. Bridge mirrors remote_claude command/session events onto the
     in-process kernel subscription broker.
  3. Watcher subscribes to ``remote_claude:machine:<machine>`` for
     each configured machine. When a command we registered transitions
     to a state where session_id is filled in, watcher records the
     mapping ``session_id -> op_context`` and spawns a session
     subscription task for ``remote_claude:session:<session_id>``.
  4. Session task waits for ``claude.event`` envelopes whose inner
     stream-json frame is ``{"type": "result", "subtype": "success",
     "result": "<final text>"}``. That final text is posted as
     speech.claim back into the originating v2 op.

V1 limitations:
  - tool-use / multi-turn runs aren't synthesized -- we wait for the
    'result' frame which claude emits at end of one turn.
  - 'claude.exit' without a prior 'result' frame means no reply will
    come (the run died without finishing). We close the session task
    silently in that case.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select

from ...behaviors.chat.conversation_schemas import SpeechActSubmitRequest
from ...behaviors.chat.models import ChatConversationModel
from ...kernel.storage import session_scope
from ...behaviors.remote_claude.state_service import (
    remote_claude_machine_space_id,
    remote_claude_session_space_id,
)

logger = logging.getLogger("opscure.agent.reply_watcher")


class RemoteClaudeReplyWatcher:
    """One watcher per bridge instance covers any number of machines.

    Lifecycle: start() → run_forever() (one task per machine) → stop()
    cancels all tasks. PCClaudeBrain calls register_dispatch() on every
    run dispatch so the watcher knows which operation a future result
    belongs to.
    """

    def __init__(
        self,
        *,
        broker,
        chat_service,
        machine_ids: list[str],
    ) -> None:
        self._broker = broker
        self._chat = chat_service
        self._machine_ids = list(machine_ids)
        self._stopping = False
        self._machine_tasks: list[asyncio.Task] = []
        self._session_tasks: dict[str, asyncio.Task] = {}
        # Set by PCClaudeBrain at enqueue time. Each entry is a small
        # dict {operation_id, actor_handle, machine_id}.
        self._cmd_to_op: dict[str, dict[str, Any]] = {}
        # Filled in once we observe the command in a machine event with
        # session_id populated. Drained when we post the reply.
        self._session_to_op: dict[str, dict[str, Any]] = {}

    # ---- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        for machine_id in self._machine_ids:
            task = asyncio.create_task(
                self._machine_loop(machine_id),
                name=f"reply-watcher:machine:{machine_id}",
            )
            self._machine_tasks.append(task)
        logger.info(
            "remote_claude reply watcher started for machines=%s",
            self._machine_ids,
        )

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._machine_tasks) + list(self._session_tasks.values()):
            task.cancel()
        for task in list(self._machine_tasks) + list(self._session_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._machine_tasks.clear()
        self._session_tasks.clear()

    # ---- registration (called by PCClaudeBrain) ----------------------

    def register_dispatch(
        self,
        *,
        command_id: str,
        machine_id: str,
        operation_id: str,
        actor_handle: str,
    ) -> None:
        self._cmd_to_op[command_id] = {
            "operation_id": operation_id,
            "actor_handle": actor_handle,
            "machine_id": machine_id,
        }
        logger.debug(
            "watcher.register_dispatch cmd=%s op=%s actor=%s",
            command_id, operation_id, actor_handle,
        )

    # ---- machine-level loop ------------------------------------------

    async def _machine_loop(self, machine_id: str) -> None:
        space = remote_claude_machine_space_id(machine_id)
        sub = self._broker.subscribe(
            space_id=space,
            subscriber_id=f"reply-watcher:machine:{machine_id}",
        )
        try:
            while not self._stopping:
                envelope = await sub.next_event(timeout_seconds=15.0)
                if envelope is None:
                    continue  # heartbeat tick
                try:
                    self._handle_machine_event(envelope, machine_id)
                except Exception:  # noqa: BLE001
                    logger.exception("watcher: machine event handler failed")
        finally:
            sub.close()

    def _handle_machine_event(self, envelope, machine_id: str) -> None:
        # remote_claude.command.<status> events carry the command + session_id
        if not envelope.event.kind.startswith("remote_claude.command."):
            return
        try:
            payload = json.loads(envelope.event.content)
        except (ValueError, TypeError):
            return
        command = payload.get("command") or {}
        cid = command.get("commandId")
        session_id = command.get("sessionId")
        if not cid or not session_id:
            return
        ctx = self._cmd_to_op.pop(cid, None)
        if ctx is None:
            return  # not one of ours
        ctx["session_id"] = session_id
        self._session_to_op[session_id] = ctx
        if session_id not in self._session_tasks:
            self._session_tasks[session_id] = asyncio.create_task(
                self._session_loop(machine_id, session_id),
                name=f"reply-watcher:session:{session_id}",
            )

    # ---- session-level loop (one per active run) ---------------------

    async def _session_loop(self, machine_id: str, session_id: str) -> None:
        space = remote_claude_session_space_id(session_id)
        sub = self._broker.subscribe(
            space_id=space,
            subscriber_id=f"reply-watcher:session:{session_id}",
        )
        try:
            while not self._stopping:
                envelope = await sub.next_event(timeout_seconds=60.0)
                if envelope is None:
                    continue
                kind = envelope.event.kind
                if kind == "claude.event":
                    if self._handle_stream_event(session_id, envelope):
                        return  # done with this session
                elif kind == "claude.exit":
                    # Run died without emitting a result frame. Drop
                    # the pending op entry; no reply will come.
                    self._session_to_op.pop(session_id, None)
                    return
        finally:
            sub.close()
            self._session_tasks.pop(session_id, None)

    def _handle_stream_event(self, session_id: str, envelope) -> bool:
        """Returns True if we posted a reply (loop should exit)."""
        try:
            payload = json.loads(envelope.event.content)
        except (ValueError, TypeError):
            return False
        inner = payload.get("event")
        if not isinstance(inner, dict):
            return False
        if inner.get("type") != "result":
            return False
        if inner.get("subtype") not in ("success", None, ""):
            # Failure / error result -- still terminal, but don't post.
            self._session_to_op.pop(session_id, None)
            return True
        text = str(inner.get("result") or "").strip()
        if not text:
            self._session_to_op.pop(session_id, None)
            return True
        ctx = self._session_to_op.pop(session_id, None)
        if ctx is None:
            return True
        self._post_reply(ctx, text)
        return True

    # ---- post back into v2 op ----------------------------------------

    def _post_reply(self, ctx: dict[str, Any], text: str) -> None:
        op_id = ctx.get("operation_id")
        actor_handle = ctx.get("actor_handle") or "@bridge-agent"
        if not op_id:
            return
        v1_id = self._operation_id_to_v1_id(op_id)
        if v1_id is None:
            logger.warning("watcher: op %s has no v1 mirror; dropping reply", op_id)
            return
        try:
            self._chat.submit_speech(
                conversation_id=v1_id,
                request=SpeechActSubmitRequest(
                    actor_name=actor_handle.lstrip("@"),
                    kind="claim",
                    content=text,
                ),
            )
            logger.info(
                "watcher: posted reply to op=%s actor=%s len=%d",
                op_id, actor_handle, len(text),
            )
        except Exception:  # noqa: BLE001
            logger.exception("watcher: submit_speech failed for op=%s", op_id)

    @staticmethod
    def _operation_id_to_v1_id(operation_id: str) -> str | None:
        with session_scope() as db:
            row = db.scalar(
                select(ChatConversationModel)
                .where(ChatConversationModel.v2_operation_id == operation_id)
                .limit(1)
            )
            return row.id if row else None
