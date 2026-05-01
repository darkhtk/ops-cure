"""Task lifecycle binding for ``Conversation(kind=task)``.

A task-kind conversation in a chat room is the protocol-level unit of
"work to be done". It owns a ``RemoteTaskModel`` row through
``RemoteTaskService`` for the heavy lifting (lease tokens, evidence,
heartbeat metrics, approval — all the contracts already used by
remote_codex / remote_claude). This coordinator is the small layer that
wires those primitives back into the chat conversation:

- claim/heartbeat/evidence are forwarded to ``RemoteTaskService`` and
  echoed into the bound conversation as typed ``chat.task.*`` events
- complete/fail terminate the conversation by calling
  ``ChatConversationService.close_conversation`` with
  ``bypass_task_guard=True``; the resolution string mirrors the task
  status (``completed`` / ``failed``)
- the conversation's ``expected_speaker`` slot tracks the task owner so
  the room knows whose turn it is (consumed by PR3 turn rendering)

Approval, interrupt, and notes are still on RemoteTaskService and will
get coordinator wrappers in PR2.5/PR3 once the basic lifecycle is solid.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ...kernel.events import (
    EventEnvelope,
    make_message_envelope,
    publish_envelope,
)
from ...kernel.storage import session_scope
from ...schemas import (
    RemoteTaskClaimRequest,
    RemoteTaskCompleteRequest,
    RemoteTaskEvidenceRequest,
    RemoteTaskFailRequest,
    RemoteTaskHeartbeatRequest,
    RemoteTaskSummaryResponse,
)
from ...services.remote_task_service import RemoteTaskService
from .conversation_schemas import (
    ChatTaskClaimRequest,
    ChatTaskCompleteRequest,
    ChatTaskEvidenceRequest,
    ChatTaskFailRequest,
    ChatTaskHeartbeatRequest,
    ChatTaskStateResponse,
    ConversationSummary,
)
from .conversation_service import ChatConversationService
from .models import (
    CONVERSATION_KIND_TASK,
    CONVERSATION_STATE_CLOSED,
    ChatConversationModel,
    ChatMessageModel,
)


EVENT_TASK_CLAIMED = "chat.task.claimed"
EVENT_TASK_HEARTBEAT = "chat.task.heartbeat"
EVENT_TASK_EVIDENCE = "chat.task.evidence"
EVENT_TASK_COMPLETED = "chat.task.completed"
EVENT_TASK_FAILED = "chat.task.failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatTaskBindingError(LookupError):
    """Raised when a conversation has no bound task or the wrong kind."""


class ChatTaskCoordinator:
    def __init__(
        self,
        *,
        conversation_service: ChatConversationService,
        remote_task_service: RemoteTaskService,
        subscription_broker: Any | None = None,
    ) -> None:
        self._conversations = conversation_service
        self._remote = remote_task_service
        self._broker = subscription_broker

    # -------- public lifecycle ---------------------------------------------

    def claim(
        self,
        *,
        conversation_id: str,
        request: ChatTaskClaimRequest,
    ) -> ChatTaskStateResponse:
        task_id = self._require_bound_task(conversation_id)
        self._conversations.metrics.record_task_claimed()
        task = self._remote.claim_task(
            task_id,
            RemoteTaskClaimRequest(
                actor_id=request.actor_name,
                lease_seconds=request.lease_seconds,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_CLAIMED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "leaseExpiresAt": task.current_assignment.lease_expires_at.isoformat()
                if task.current_assignment is not None
                else None,
            },
            new_owner=request.actor_name,
            new_expected_speaker=request.actor_name,
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    def heartbeat(
        self,
        *,
        conversation_id: str,
        request: ChatTaskHeartbeatRequest,
    ) -> ChatTaskStateResponse:
        task_id = self._require_bound_task(conversation_id)
        self._conversations.metrics.record_task_heartbeat()
        task = self._remote.heartbeat_task(
            task_id,
            RemoteTaskHeartbeatRequest(
                actor_id=request.actor_name,
                lease_token=request.lease_token,
                phase=request.phase,
                summary=request.summary,
                commands_run_count=request.commands_run_count,
                files_read_count=request.files_read_count,
                files_modified_count=request.files_modified_count,
                tests_run_count=request.tests_run_count,
                lease_seconds=request.lease_seconds,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_HEARTBEAT,
            payload={
                "taskId": task.id,
                "status": task.status,
                "phase": request.phase,
                "summary": request.summary,
                "metrics": {
                    "commandsRunCount": request.commands_run_count,
                    "filesReadCount": request.files_read_count,
                    "filesModifiedCount": request.files_modified_count,
                    "testsRunCount": request.tests_run_count,
                },
            },
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    def add_evidence(
        self,
        *,
        conversation_id: str,
        request: ChatTaskEvidenceRequest,
    ) -> ChatTaskStateResponse:
        task_id = self._require_bound_task(conversation_id)
        self._conversations.metrics.record_task_evidence()
        task = self._remote.add_evidence(
            task_id,
            RemoteTaskEvidenceRequest(
                actor_id=request.actor_name,
                kind=request.kind,
                summary=request.summary,
                payload=request.payload,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_EVIDENCE,
            payload={
                "taskId": task.id,
                "status": task.status,
                "evidenceKind": request.kind,
                "summary": request.summary,
            },
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    def complete(
        self,
        *,
        conversation_id: str,
        request: ChatTaskCompleteRequest,
    ) -> ChatTaskStateResponse:
        task_id = self._require_bound_task(conversation_id)
        self._conversations.metrics.record_task_completed()
        task = self._remote.complete_task(
            task_id,
            RemoteTaskCompleteRequest(
                actor_id=request.actor_name,
                lease_token=request.lease_token,
                summary=request.summary,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_COMPLETED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "summary": request.summary,
            },
            new_expected_speaker_to_none=True,
        )
        self._conversations.close_conversation(
            conversation_id=conversation_id,
            closed_by=request.actor_name,
            resolution="completed",
            summary=request.summary,
            bypass_task_guard=True,
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    def fail(
        self,
        *,
        conversation_id: str,
        request: ChatTaskFailRequest,
    ) -> ChatTaskStateResponse:
        task_id = self._require_bound_task(conversation_id)
        self._conversations.metrics.record_task_failed()
        task = self._remote.fail_task(
            task_id,
            RemoteTaskFailRequest(
                actor_id=request.actor_name,
                lease_token=request.lease_token,
                error_text=request.error_text,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_FAILED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "errorText": request.error_text,
            },
            new_expected_speaker_to_none=True,
        )
        self._conversations.close_conversation(
            conversation_id=conversation_id,
            closed_by=request.actor_name,
            resolution="failed",
            summary=request.error_text,
            bypass_task_guard=True,
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    # -------- internals -----------------------------------------------------

    def _require_bound_task(self, conversation_id: str) -> str:
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatTaskBindingError(
                    f"conversation {conversation_id} not found",
                )
            if row.kind != CONVERSATION_KIND_TASK or not row.bound_task_id:
                raise ChatTaskBindingError(
                    f"conversation {conversation_id} is not a task conversation",
                )
            return row.bound_task_id

    def _update_owner_and_emit(
        self,
        *,
        conversation_id: str,
        actor_name: str,
        event_kind: str,
        payload: dict[str, Any],
        new_owner: str | None = None,
        new_expected_speaker: str | None = None,
        new_expected_speaker_to_none: bool = False,
    ) -> None:
        envelope: EventEnvelope | None = None
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                return
            now = _utcnow()
            if new_owner is not None:
                row.owner_actor = new_owner
            if new_expected_speaker_to_none:
                row.expected_speaker = None
            elif new_expected_speaker is not None:
                row.expected_speaker = new_expected_speaker
            row.last_speech_at = now
            row.speech_count = (row.speech_count or 0) + 1
            row.updated_at = now

            event_message = ChatMessageModel(
                thread_id=row.thread_id,
                conversation_id=row.id,
                actor_name=actor_name,
                event_kind=event_kind,
                content=json.dumps(payload, ensure_ascii=False),
            )
            db.add(event_message)
            db.flush()
            envelope = make_message_envelope(
                space_id=row.thread_id,
                message=event_message,
            )

        publish_envelope(self._broker, envelope)

    def _build_response(
        self,
        *,
        conversation_id: str,
        task: RemoteTaskSummaryResponse,
    ) -> ChatTaskStateResponse:
        # Re-read the conversation summary so the response reflects any
        # auto-close / owner update we just performed.
        detail = self._conversations.get_conversation(
            conversation_id=conversation_id,
            recent=1,
        )
        return ChatTaskStateResponse(
            conversation=detail.conversation,
            task=task.model_dump(mode="json"),
        )
