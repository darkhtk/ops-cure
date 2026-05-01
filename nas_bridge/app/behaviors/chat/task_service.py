"""Chat behavior task lifecycle service.

A chat thread becomes an "AI 협업룸" the moment it can hold canonical
tasks: any participant (human or AI) can create a queued task in the
thread, another participant can claim it with a lease, the owner emits
heartbeats and structured evidence, and the lifecycle terminates with
complete/fail. Approvals and notes are also exposed for explicit
coordination gates.

Implementation is a thin facade over ``RemoteTaskService`` so we share
the battle-tested lease/heartbeat/evidence/approval machinery instead of
duplicating it. The mapping is:

- ``machine_id`` is fixed to the sentinel ``"chat"`` for every chat task.
- ``thread_id`` is the chat thread's internal UUID (``ChatThreadModel.id``),
  which is also the chat behavior's kernel space id.
- ``actor_id`` carries the chat ``actor_name`` since names are
  thread-scoped and unique inside one room.

Each lifecycle transition writes a ``ChatMessageModel`` row with a
``chat.task.*`` ``event_kind`` so the existing chat events stream
(SSE, kernel events) carries them automatically and the kernel
``EventEnvelope`` cursor replay continues to work after disconnect.
Task events do **not** bump the chat thread's ``turn_count`` because
they are not conversational turns.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ...kernel.events import EventEnvelope, EventSummary, encode_event_cursor
from ...kernel.storage import session_scope
from ...schemas import (
    RemoteTaskApprovalRequest,
    RemoteTaskApprovalResolveRequest,
    RemoteTaskClaimRequest,
    RemoteTaskCompleteRequest,
    RemoteTaskCreateRequest,
    RemoteTaskEvidenceRequest,
    RemoteTaskFailRequest,
    RemoteTaskHeartbeatRequest,
    RemoteTaskInterruptRequest,
    RemoteTaskNoteRequest,
    RemoteTaskSummaryResponse,
)
from ...services.remote_task_service import RemoteTaskService
from .models import ChatMessageModel, ChatThreadModel


CHAT_TASK_MACHINE_ID = "chat"

EVENT_TASK_CREATED = "chat.task.created"
EVENT_TASK_CLAIMED = "chat.task.claimed"
EVENT_TASK_HEARTBEAT = "chat.task.heartbeat"
EVENT_TASK_EVIDENCE = "chat.task.evidence"
EVENT_TASK_COMPLETED = "chat.task.completed"
EVENT_TASK_FAILED = "chat.task.failed"
EVENT_TASK_INTERRUPTED = "chat.task.interrupted"
EVENT_TASK_APPROVAL_REQUESTED = "chat.task.approval_requested"
EVENT_TASK_APPROVAL_RESOLVED = "chat.task.approval_resolved"
EVENT_TASK_NOTE = "chat.task.note"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatThreadNotFoundError(LookupError):
    """Raised when a chat thread cannot be located by discord_thread_id."""


class ChatTaskService:
    def __init__(
        self,
        *,
        remote_task_service: RemoteTaskService,
        subscription_broker: Any | None = None,
    ) -> None:
        self._remote = remote_task_service
        self._broker = subscription_broker

    # -------- thread-scoped operations --------------------------------------

    def create_task(
        self,
        *,
        discord_thread_id: str,
        objective: str,
        created_by: str,
        success_criteria: dict[str, Any] | None = None,
        priority: str = "normal",
        origin_message_id: str | None = None,
    ) -> RemoteTaskSummaryResponse:
        chat_thread_id = self._resolve_chat_thread_id(discord_thread_id)
        task = self._remote.create_task(
            RemoteTaskCreateRequest(
                machine_id=CHAT_TASK_MACHINE_ID,
                thread_id=chat_thread_id,
                objective=objective,
                success_criteria=success_criteria or {},
                origin_surface="chat",
                origin_message_id=origin_message_id,
                priority=priority,
                created_by=created_by,
            ),
        )
        self._emit(
            chat_thread_id=chat_thread_id,
            actor_name=created_by,
            kind=EVENT_TASK_CREATED,
            payload={
                "taskId": task.id,
                "objective": task.objective,
                "priority": task.priority,
                "status": task.status,
            },
        )
        return task

    def list_tasks(
        self,
        *,
        discord_thread_id: str,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[RemoteTaskSummaryResponse]:
        chat_thread_id = self._resolve_chat_thread_id(discord_thread_id)
        return self._remote.list_tasks(
            machine_id=CHAT_TASK_MACHINE_ID,
            thread_id=chat_thread_id,
            statuses=statuses,
            limit=limit,
        )

    # -------- task-scoped operations ----------------------------------------

    def get_task(self, task_id: str) -> RemoteTaskSummaryResponse:
        return self._remote.get_task(task_id)

    def claim_task(
        self,
        *,
        task_id: str,
        actor_name: str,
        lease_seconds: int = 120,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.claim_task(
            task_id,
            RemoteTaskClaimRequest(actor_id=actor_name, lease_seconds=lease_seconds),
        )
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_CLAIMED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "leaseExpiresAt": task.current_assignment.lease_expires_at.isoformat()
                if task.current_assignment is not None
                else None,
            },
        )
        return task

    def heartbeat_task(
        self,
        *,
        task_id: str,
        actor_name: str,
        lease_token: str,
        phase: str,
        summary: str | None = None,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
        lease_seconds: int = 120,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.heartbeat_task(
            task_id,
            RemoteTaskHeartbeatRequest(
                actor_id=actor_name,
                lease_token=lease_token,
                phase=phase,
                summary=summary,
                commands_run_count=commands_run_count,
                files_read_count=files_read_count,
                files_modified_count=files_modified_count,
                tests_run_count=tests_run_count,
                lease_seconds=lease_seconds,
            ),
        )
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_HEARTBEAT,
            payload={
                "taskId": task.id,
                "status": task.status,
                "phase": phase,
                "summary": summary,
                "metrics": {
                    "commandsRunCount": commands_run_count,
                    "filesReadCount": files_read_count,
                    "filesModifiedCount": files_modified_count,
                    "testsRunCount": tests_run_count,
                },
            },
        )
        return task

    def add_evidence(
        self,
        *,
        task_id: str,
        actor_name: str,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.add_evidence(
            task_id,
            RemoteTaskEvidenceRequest(
                actor_id=actor_name,
                kind=kind,
                summary=summary,
                payload=payload or {},
            ),
        )
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_EVIDENCE,
            payload={
                "taskId": task.id,
                "status": task.status,
                "evidenceKind": kind,
                "summary": summary,
            },
        )
        return task

    def complete_task(
        self,
        *,
        task_id: str,
        actor_name: str,
        lease_token: str,
        summary: str | None = None,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.complete_task(
            task_id,
            RemoteTaskCompleteRequest(
                actor_id=actor_name,
                lease_token=lease_token,
                summary=summary,
            ),
        )
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_COMPLETED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "summary": summary,
            },
        )
        return task

    def fail_task(
        self,
        *,
        task_id: str,
        actor_name: str,
        lease_token: str,
        error_text: str,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.fail_task(
            task_id,
            RemoteTaskFailRequest(
                actor_id=actor_name,
                lease_token=lease_token,
                error_text=error_text,
            ),
        )
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_FAILED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "errorText": error_text,
            },
        )
        return task

    def interrupt_task(
        self,
        *,
        task_id: str,
        actor_name: str,
        lease_token: str,
        note: str | None = None,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.interrupt_task(
            task_id,
            RemoteTaskInterruptRequest(
                actor_id=actor_name,
                lease_token=lease_token,
                note=note,
            ),
        )
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_INTERRUPTED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "note": note,
            },
        )
        return task

    def request_approval(
        self,
        *,
        task_id: str,
        actor_name: str,
        lease_token: str,
        reason: str,
        note: str | None = None,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.request_approval(
            task_id,
            RemoteTaskApprovalRequest(
                actor_id=actor_name,
                lease_token=lease_token,
                reason=reason,
                note=note,
            ),
        )
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_APPROVAL_REQUESTED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "reason": reason,
                "note": note,
            },
        )
        return task

    def resolve_approval(
        self,
        *,
        task_id: str,
        resolved_by: str,
        resolution: str,
        note: str | None = None,
    ) -> RemoteTaskSummaryResponse:
        task = self._remote.resolve_approval(
            task_id,
            RemoteTaskApprovalResolveRequest(
                resolved_by=resolved_by,
                resolution=resolution,
                note=note,
            ),
        )
        self._emit_for_task(
            task,
            actor_name=resolved_by,
            kind=EVENT_TASK_APPROVAL_RESOLVED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "resolution": resolution,
                "note": note,
            },
        )
        return task

    def add_note(
        self,
        *,
        task_id: str,
        actor_name: str,
        kind: str,
        content: str,
    ) -> dict[str, Any]:
        note = self._remote.add_note(
            task_id,
            RemoteTaskNoteRequest(actor_id=actor_name, kind=kind, content=content),
        )
        task = self._remote.get_task(task_id)
        self._emit_for_task(
            task,
            actor_name=actor_name,
            kind=EVENT_TASK_NOTE,
            payload={
                "taskId": task.id,
                "noteKind": kind,
                "content": content,
            },
        )
        return {
            "id": note.id,
            "actorId": note.actor_id,
            "kind": note.kind,
            "content": note.content,
            "createdAt": note.created_at.isoformat(),
        }

    # -------- internals -----------------------------------------------------

    def _resolve_chat_thread_id(self, discord_thread_id: str) -> str:
        with session_scope() as db:
            row = db.scalar(
                select(ChatThreadModel).where(
                    ChatThreadModel.discord_thread_id == discord_thread_id,
                ),
            )
            if row is None:
                raise ChatThreadNotFoundError(discord_thread_id)
            return row.id

    def _emit_for_task(
        self,
        task: RemoteTaskSummaryResponse,
        *,
        actor_name: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        self._emit(
            chat_thread_id=task.thread_id,
            actor_name=actor_name,
            kind=kind,
            payload=payload,
        )

    def _emit(
        self,
        *,
        chat_thread_id: str,
        actor_name: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False)
        envelope: EventEnvelope | None = None
        with session_scope() as db:
            thread_row = db.get(ChatThreadModel, chat_thread_id)
            if thread_row is None:
                # Task was created against a chat thread that no longer
                # exists. The remote task itself is still canonical, but we
                # have nowhere to render the event — drop quietly.
                return
            message = ChatMessageModel(
                thread_id=chat_thread_id,
                actor_name=actor_name,
                event_kind=kind,
                content=content,
            )
            db.add(message)
            db.flush()
            envelope = EventEnvelope(
                cursor=encode_event_cursor(
                    created_at=message.created_at,
                    event_id=message.id,
                ),
                space_id=chat_thread_id,
                event=EventSummary(
                    id=message.id,
                    kind=kind,
                    actor_name=actor_name,
                    content=content,
                    created_at=message.created_at,
                ),
            )
        if envelope is not None and self._broker is not None:
            self._broker.publish(space_id=envelope.space_id, item=envelope)
