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

from sqlalchemy import func, select

from ...kernel.events import (
    EventEnvelope,
    make_message_envelope,
    publish_envelope,
)
from ...kernel.storage import session_scope
from ...kernel.v2 import OperationMirror as _OperationMirror
from ...schemas import (
    RemoteTaskApprovalRequest,
    RemoteTaskApprovalResolveRequest,
    RemoteTaskClaimRequest,
    RemoteTaskCompleteRequest,
    RemoteTaskEvidenceRequest,
    RemoteTaskFailRequest,
    RemoteTaskHeartbeatRequest,
    RemoteTaskInterruptRequest,
    RemoteTaskNoteRequest,
    RemoteTaskSummaryResponse,
)
from ...services.remote_task_service import RemoteTaskService
from .conversation_schemas import (
    ChatTaskApprovalRequest,
    ChatTaskApprovalResolveRequest,
    ChatTaskClaimRequest,
    ChatTaskCompleteRequest,
    ChatTaskEvidenceRequest,
    ChatTaskFailRequest,
    ChatTaskHeartbeatRequest,
    ChatTaskInterruptRequest,
    ChatTaskNoteRequest,
    ChatTaskNoteResponse,
    ChatTaskStateResponse,
    ConversationSummary,
)
from .conversation_service import ChatConversationService, _mirror_v1_message_to_v2
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
EVENT_TASK_INTERRUPTED = "chat.task.interrupted"
EVENT_TASK_APPROVAL_REQUESTED = "chat.task.approval_requested"
EVENT_TASK_APPROVAL_RESOLVED = "chat.task.approval_resolved"
EVENT_TASK_NOTE = "chat.task.note"


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
            new_v2_state="claimed",
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
        # PR-hardening: evidence path is now lease-gated. Previously
        # ``add_evidence`` accepted any actor_name without verifying
        # they held the lease, letting unrelated actors plant
        # evidence on tasks they had no part in (failure-mode
        # scenario #07).
        current = self._remote.get_task(task_id)
        if current.current_assignment is None:
            raise ChatTaskBindingError(
                f"task {task_id} has no active assignment; cannot add evidence",
            )
        if (
            current.current_assignment.actor_id != request.actor_name
            or current.current_assignment.lease_token != request.lease_token
        ):
            raise ChatTaskBindingError(
                f"actor or lease_token mismatch: only the current lease holder "
                f"({current.current_assignment.actor_id}) may add evidence",
            )
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
        # F6: pass artifact through if the caller embedded one in the
        # evidence payload. The v2 OperationArtifact row gets pinned to
        # this evidence event id; v1 has no artifact concept and stays
        # narrative-only.
        artifact_meta = None
        if isinstance(request.payload.get("artifact"), dict):
            artifact_meta = dict(request.payload["artifact"])
        # First evidence flips claimed -> executing. Subsequent evidence
        # is idempotent (transition_state is no-op when already in target).
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
            artifact=artifact_meta,
            new_v2_state="executing",
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    def complete(
        self,
        *,
        conversation_id: str,
        request: ChatTaskCompleteRequest,
    ) -> ChatTaskStateResponse:
        task_id = self._require_bound_task(conversation_id)
        # PR-hardening (failure-mode #08): the agent contract requires
        # at least one evidence row before claiming a task complete.
        # Previously this was documentation only; now it is enforced
        # at the coordinator. The fail/interrupt paths intentionally
        # bypass this check -- you can fail a task with no evidence
        # because failure isn't a claim of work done.
        from .models import ChatMessageModel  # local to keep top imports tidy
        with session_scope() as db:
            ev_count = db.scalar(
                select(func.count())
                .select_from(ChatMessageModel)
                .where(ChatMessageModel.conversation_id == conversation_id)
                .where(ChatMessageModel.event_kind == "chat.task.evidence")
            ) or 0
        if ev_count == 0:
            raise ChatTaskBindingError(
                f"task complete requires at least one evidence row; "
                f"call add_evidence first (or fail/interrupt instead)",
            )
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

    # -------- approval / interrupt / note (PR14) ---------------------------

    def request_approval(
        self,
        *,
        conversation_id: str,
        request: ChatTaskApprovalRequest,
    ) -> ChatTaskStateResponse:
        """Owner asks for human approval before continuing. Task state
        moves to ``blocked_approval`` until ``resolve_approval`` is
        called. Conversation stays open (the bound task is still
        active; just gated)."""
        task_id = self._require_bound_task(conversation_id)
        task = self._remote.request_approval(
            task_id,
            RemoteTaskApprovalRequest(
                actor_id=request.actor_name,
                lease_token=request.lease_token,
                reason=request.reason,
                note=request.note,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_APPROVAL_REQUESTED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "reason": request.reason,
                "note": request.note,
            },
            new_v2_state="blocked_approval",
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    def resolve_approval(
        self,
        *,
        conversation_id: str,
        request: ChatTaskApprovalResolveRequest,
    ) -> ChatTaskStateResponse:
        """Resolve a pending approval. resolution must be
        'approved' or 'denied'. On approved, task returns to
        executing state; on denied, the conversation is auto-closed
        with resolution=cancelled (the task can no longer proceed)."""
        task_id = self._require_bound_task(conversation_id)
        task = self._remote.resolve_approval(
            task_id,
            RemoteTaskApprovalResolveRequest(
                resolved_by=request.resolved_by,
                resolution=request.resolution,
                note=request.note,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.resolved_by,
            event_kind=EVENT_TASK_APPROVAL_RESOLVED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "resolution": request.resolution,
                "note": request.note,
            },
            new_v2_state="executing" if request.resolution == "approved" else None,
        )
        # On 'denied', auto-close the bound conversation as cancelled.
        if request.resolution == "denied":
            self._conversations.close_conversation(
                conversation_id=conversation_id,
                closed_by=request.resolved_by,
                resolution="cancelled",
                summary=f"approval denied: {request.note or '(no note)'}",
                bypass_task_guard=True,
            )
        return self._build_response(conversation_id=conversation_id, task=task)

    def interrupt(
        self,
        *,
        conversation_id: str,
        request: ChatTaskInterruptRequest,
    ) -> ChatTaskStateResponse:
        """Owner explicitly interrupts a running task without failing
        it. The task state moves to ``interrupted``; the lease is
        kept so the same actor can resume by claiming again, or
        another actor can take over after lease expiry. Conversation
        stays open."""
        task_id = self._require_bound_task(conversation_id)
        task = self._remote.interrupt_task(
            task_id,
            RemoteTaskInterruptRequest(
                actor_id=request.actor_name,
                lease_token=request.lease_token,
                note=request.note,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_INTERRUPTED,
            payload={
                "taskId": task.id,
                "status": task.status,
                "note": request.note,
            },
        )
        return self._build_response(conversation_id=conversation_id, task=task)

    def add_note(
        self,
        *,
        conversation_id: str,
        request: ChatTaskNoteRequest,
    ) -> ChatTaskNoteResponse:
        """Add a coordination note attached to the bound task. Notes
        are observation-only -- they don't change task state. Useful
        for cross-checks ('I reviewed the PR and...'), questions
        ('@bob can you confirm the migration impact?'), or
        handoff context that doesn't fit a speech act."""
        task_id = self._require_bound_task(conversation_id)
        note = self._remote.add_note(
            task_id,
            RemoteTaskNoteRequest(
                actor_id=request.actor_name,
                kind=request.kind,
                content=request.content,
            ),
        )
        self._update_owner_and_emit(
            conversation_id=conversation_id,
            actor_name=request.actor_name,
            event_kind=EVENT_TASK_NOTE,
            payload={
                "taskId": task_id,
                "noteKind": request.kind,
                "content": request.content,
            },
        )
        detail = self._conversations.get_conversation(
            conversation_id=conversation_id, recent=1,
        )
        return ChatTaskNoteResponse(
            conversation=detail.conversation,
            note={
                "id": note.id,
                "actorId": note.actor_id,
                "kind": note.kind,
                "content": note.content,
                "createdAt": note.created_at.isoformat(),
            },
        )

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
        artifact: dict[str, Any] | None = None,
        new_v2_state: str | None = None,
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
            # F4 dual-write: mirror task lifecycle event into v2.
            mirror = _OperationMirror()
            _mirror_v1_message_to_v2(db, event_message, row, mirror)
            # F6 dual-write: artifact tied to this event (evidence path).
            if artifact:
                mirror.attach_artifact(
                    db,
                    v2_operation_id=row.v2_operation_id,
                    v2_event_id=event_message.v2_event_id,
                    artifact=artifact,
                )
            # G2-followup (#2 fix): wire task lifecycle into operations_v2.state
            # so /v2/inbox?state=executing & friends actually return what
            # callers expect. State machine assertion in mirror catches
            # invalid transitions (programmer error in mapping below).
            if new_v2_state is not None and row.v2_operation_id is not None:
                mirror.transition_state(
                    db,
                    v2_operation_id=row.v2_operation_id,
                    to_state=new_v2_state,
                )
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
