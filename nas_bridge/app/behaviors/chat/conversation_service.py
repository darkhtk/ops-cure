"""Chat conversation protocol service.

Why this exists:
    AI 협업룸 needs explicit *open* and *close* of collaboration units,
    not just a free-form message stream. Conversations carry typed
    intent (inquiry / proposal / task) and must reach a resolution.
    Casual chat lives in the always-open ``general`` conversation per
    room so every speech act has a home.

Design notes:
    * Each lifecycle transition (opened / closed / speech / address) is
      persisted as a ``ChatMessageModel`` row with a discriminating
      ``event_kind``. The existing chat kernel events stream already
      sources from this table, so SSE resume/replay continues to work
      unchanged for new event kinds.
    * ``general`` conversations are forced ``is_general=True`` and
      cannot be closed (raised as ``ValueError``); they are created on
      first need by ``ensure_general()`` and backfilled lazily.
    * The service is intentionally agnostic of Discord transport. The
      higher ``ChatBehaviorService`` and Discord bindings are responsible
      for human-readable rendering.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone  # noqa: F401  -- used in sweep timing math
from typing import Any

from sqlalchemy import func, or_, select, update

from ...kernel.events import EventEnvelope, EventSummary, encode_event_cursor
from ...kernel.storage import session_scope
from ...kernel.operation_service import KernelOperationService as RemoteTaskService
from ...schemas import RemoteTaskCreateRequest
from ...transcript_service import sanitize_text
from .conversation_schemas import (
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationOpenRequest,
    ConversationSummary,
    SpeechActSubmitRequest,
    SpeechActSummary,
    is_resolution_allowed,
)
from .models import (
    CONVERSATION_KIND_GENERAL,
    CONVERSATION_KIND_TASK,
    CONVERSATION_STATE_CLOSED,
    CONVERSATION_STATE_OPEN,
    ChatConversationModel,
    ChatMessageModel,
    ChatThreadModel,
)


# RemoteTaskService keys tasks by (machine_id, thread_id). Chat tasks share a
# single sentinel machine; the chat thread's internal UUID supplies the
# scope. Once Operation is promoted to the kernel (Candidate 2 in
# generic-kernel-promotion-candidates.md) this sentinel goes away.
CHAT_TASK_MACHINE_ID = "chat"

# Statuses RemoteTaskService treats as "still owning the conversation". A
# manual ``close_conversation`` against a task-bound conversation in any of
# these states is rejected so the lifecycle stays consistent — only
# ChatTaskCoordinator's complete/fail/cancel paths can close them.
TASK_NON_TERMINAL_STATUSES = frozenset(
    {
        "queued",
        "claimed",
        "executing",
        "blocked_approval",
        "verifying",
        "interrupted",
        "stalled",
    }
)


GENERAL_TITLE = "General"
GENERAL_INTENT = "Casual chat and unstructured updates."

EVENT_CONVERSATION_OPENED = "chat.conversation.opened"
EVENT_CONVERSATION_CLOSED = "chat.conversation.closed"
EVENT_CONVERSATION_ADDRESSED = "chat.conversation.addressed"
EVENT_CONVERSATION_IDLE_WARNING = "chat.conversation.idle_warning"
EVENT_CONVERSATION_HANDOFF = "chat.conversation.handoff"


# Idle escalation tier multipliers (over the caller-supplied tier-1
# base). With a default 30min tier-1 these resolve to:
#   tier-1: 30min   -> emit chat.conversation.idle_warning level=1
#   tier-2: 2h      -> emit chat.conversation.idle_warning level=2
#   tier-3: 24h     -> auto-abandon (close with resolution=abandoned)
TIER_1_MULTIPLIER = 1
TIER_2_MULTIPLIER = 4
TIER_3_MULTIPLIER = 48


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_general_conversation(
    db,
    thread_row: ChatThreadModel,
) -> ChatConversationModel:
    """Module-level helper so both ChatConversationService and
    ChatBehaviorService can share the bootstrap logic for a room's
    always-open ``general`` conversation without depending on each other.
    """
    general = db.scalar(
        select(ChatConversationModel)
        .where(ChatConversationModel.thread_id == thread_row.id)
        .where(ChatConversationModel.is_general.is_(True))
        .limit(1),
    )
    if general is not None:
        return general
    general = ChatConversationModel(
        thread_id=thread_row.id,
        kind=CONVERSATION_KIND_GENERAL,
        title=GENERAL_TITLE,
        intent=GENERAL_INTENT,
        state=CONVERSATION_STATE_OPEN,
        opener_actor="system",
        owner_actor=None,
        is_general=True,
    )
    db.add(general)
    db.flush()
    return general


def _speech_event_kind(kind: str) -> str:
    return f"chat.speech.{kind}"


class ChatConversationNotFoundError(LookupError):
    """Raised when a conversation cannot be located."""


class ChatThreadNotFoundError(LookupError):
    """Raised when the parent chat thread cannot be located."""


class ChatConversationStateError(ValueError):
    """Raised when a transition is not legal for the current conversation state."""


class ChatConversationService:
    def __init__(
        self,
        *,
        subscription_broker: Any | None = None,
        remote_task_service: RemoteTaskService | None = None,
    ) -> None:
        self._broker = subscription_broker
        self._remote_task_service = remote_task_service

    # -------- thread / general bootstrap ------------------------------------

    def ensure_general(self, *, discord_thread_id: str) -> ConversationSummary:
        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)
            row = self._get_or_create_general(db, thread_row)
            return self._summary(row)

    def ensure_general_for_thread_id(self, *, thread_id: str) -> ConversationSummary:
        with session_scope() as db:
            thread_row = db.get(ChatThreadModel, thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(thread_id)
            row = self._get_or_create_general(db, thread_row)
            return self._summary(row)

    def backfill_general_conversations(self) -> int:
        """Ensure every room has a general conversation and orphan messages
        get attached to it. Idempotent and self-skipping: a single
        count() at the top exits early when there is nothing to migrate,
        which is the steady state after the first run.
        Returns the number of orphan messages newly attached."""
        with session_scope() as db:
            pending = db.scalar(
                select(func.count())
                .select_from(ChatMessageModel)
                .where(
                    or_(
                        ChatMessageModel.conversation_id.is_(None),
                        ChatMessageModel.event_kind == "message",
                    )
                )
            )
            if not pending:
                return 0

            migrated = 0
            for thread in db.scalars(select(ChatThreadModel)):
                general = self._get_or_create_general(db, thread)
                result = db.execute(
                    update(ChatMessageModel)
                    .where(ChatMessageModel.thread_id == thread.id)
                    .where(ChatMessageModel.conversation_id.is_(None))
                    .values(conversation_id=general.id),
                )
                migrated += result.rowcount or 0
                # Roll the legacy free-form "message" kind forward to
                # the typed "claim" speech kind. New writes always use
                # typed kinds so we only run this on legacy rows.
                db.execute(
                    update(ChatMessageModel)
                    .where(ChatMessageModel.thread_id == thread.id)
                    .where(ChatMessageModel.event_kind == "message")
                    .values(event_kind="claim"),
                )
        return migrated

    # -------- open / close ---------------------------------------------------

    def open_conversation(
        self,
        *,
        discord_thread_id: str,
        request: ConversationOpenRequest,
    ) -> ConversationSummary:
        # Pre-validate kind=task prerequisites before touching the DB so
        # we never half-commit (no thread row read, no task created) on
        # caller-side input errors.
        if request.kind == CONVERSATION_KIND_TASK:
            if self._remote_task_service is None:
                raise ChatConversationStateError(
                    "remote_task_service is not configured; kind=task is unavailable",
                )
            if not request.objective:
                raise ChatConversationStateError(
                    "kind=task requires an objective",
                )

        envelope: EventEnvelope | None = None
        summary: ConversationSummary
        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)
            self._get_or_create_general(db, thread_row)

            bound_task_id: str | None = None
            if request.kind == CONVERSATION_KIND_TASK:
                # RemoteTaskService runs its own session_scope; that's
                # fine because the bound RemoteTask is independent state
                # whose existence we record on the conversation row in
                # this same scope below.
                task = self._remote_task_service.create_task(
                    RemoteTaskCreateRequest(
                        machine_id=CHAT_TASK_MACHINE_ID,
                        thread_id=thread_row.id,
                        objective=request.objective,
                        success_criteria=request.success_criteria,
                        origin_surface="chat",
                        priority=request.priority,
                        created_by=request.opener_actor,
                    ),
                )
                bound_task_id = task.id

            row = ChatConversationModel(
                thread_id=thread_row.id,
                kind=request.kind,
                title=request.title,
                intent=request.intent,
                opener_actor=request.opener_actor,
                owner_actor=request.owner_actor or request.opener_actor,
                expected_speaker=request.addressed_to,
                parent_conversation_id=request.parent_conversation_id,
                bound_task_id=bound_task_id,
            )
            db.add(row)
            db.flush()

            payload = self._summary_payload(row)
            event_message = ChatMessageModel(
                thread_id=thread_row.id,
                conversation_id=row.id,
                actor_name=row.opener_actor,
                event_kind=EVENT_CONVERSATION_OPENED,
                addressed_to=row.expected_speaker,
                content=json.dumps(payload, ensure_ascii=False),
            )
            db.add(event_message)
            db.flush()
            envelope = self._envelope_for(thread_row.id, event_message)
            summary = self._summary(row)

        self._publish(envelope)
        return summary

    def close_conversation(
        self,
        *,
        conversation_id: str,
        closed_by: str,
        resolution: str,
        summary: str | None = None,
        bypass_task_guard: bool = False,
    ) -> ConversationSummary:
        envelope: EventEnvelope | None = None
        result: ConversationSummary
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)
            if row.is_general:
                raise ChatConversationStateError(
                    "general conversation cannot be closed",
                )
            if row.state == CONVERSATION_STATE_CLOSED:
                raise ChatConversationStateError(
                    f"conversation already closed (resolution={row.resolution})",
                )
            # bypass paths (task settle / system auto-abandon) carry their
            # own authority and may use kind-orthogonal resolutions like
            # ``abandoned``. User-initiated closes still must use the
            # per-kind vocabulary.
            if not bypass_task_guard and not is_resolution_allowed(
                kind=row.kind, resolution=resolution
            ):
                raise ChatConversationStateError(
                    f"resolution '{resolution}' is not valid for kind={row.kind}",
                )
            # Closure authority: only the original opener or the current
            # owner may close. The bypass_task_guard path (used by
            # ChatTaskCoordinator after a task settles) skips this check
            # because the task lease itself is the authority on that
            # branch — the lease_token already gated who could
            # complete/fail the task.
            if not bypass_task_guard:
                authorized = {row.opener_actor}
                if row.owner_actor:
                    authorized.add(row.owner_actor)
                if closed_by not in authorized:
                    raise ChatConversationStateError(
                        f"closed_by={closed_by!r} is not authorized; only opener "
                        f"({row.opener_actor!r}) or owner ({row.owner_actor!r}) may close",
                    )
            # When a task is bound and still active, only the task lifecycle
            # path (ChatTaskCoordinator.complete/fail) may close — refuse
            # manual closes so the bound RemoteTask doesn't get orphaned.
            if (
                row.bound_task_id
                and not bypass_task_guard
                and self._remote_task_service is not None
            ):
                task = self._remote_task_service.get_task(row.bound_task_id)
                if task.status in TASK_NON_TERMINAL_STATUSES:
                    raise ChatConversationStateError(
                        f"task-bound conversation cannot be manually closed while "
                        f"task is {task.status}; complete/fail the task instead",
                    )

            now = _utcnow()
            row.state = CONVERSATION_STATE_CLOSED
            row.resolution = resolution
            row.resolution_summary = summary
            row.closed_by = closed_by
            row.closed_at = now
            row.updated_at = now

            payload = self._summary_payload(row)
            event_message = ChatMessageModel(
                thread_id=row.thread_id,
                conversation_id=row.id,
                actor_name=closed_by,
                event_kind=EVENT_CONVERSATION_CLOSED,
                content=json.dumps(payload, ensure_ascii=False),
            )
            db.add(event_message)
            db.flush()
            envelope = self._envelope_for(row.thread_id, event_message)
            result = self._summary(row)

        self._publish(envelope)
        return result

    # -------- speech ---------------------------------------------------------

    def submit_speech(
        self,
        *,
        conversation_id: str,
        request: SpeechActSubmitRequest,
    ) -> SpeechActSummary:
        envelope: EventEnvelope | None = None
        summary: SpeechActSummary
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)
            if row.state == CONVERSATION_STATE_CLOSED:
                raise ChatConversationStateError(
                    "conversation is closed; reopen or start a new one",
                )

            clean = sanitize_text(request.content)
            now = _utcnow()
            message = ChatMessageModel(
                thread_id=row.thread_id,
                conversation_id=row.id,
                actor_name=request.actor_name,
                event_kind=_speech_event_kind(request.kind),
                addressed_to=request.addressed_to,
                content=clean,
            )
            db.add(message)
            db.flush()

            row.last_speech_at = now
            row.speech_count = (row.speech_count or 0) + 1

            # Soft turn-taking gauge — count speech that arrives from
            # someone OTHER than the currently-expected_speaker since the
            # last address. Reset to 0 whenever expected_speaker changes
            # (a new address effectively rebases the round).
            if request.addressed_to:
                if request.addressed_to != row.expected_speaker:
                    row.unaddressed_speech_count = 0
                row.expected_speaker = request.addressed_to
            elif request.actor_name == row.expected_speaker:
                # the expected speaker just spoke -- clear the slot and
                # reset the gauge (round resolved).
                row.expected_speaker = None
                row.unaddressed_speech_count = 0
            elif row.expected_speaker:
                # someone other than the expected speaker chimed in
                # without addressing anyone -- bump the noise gauge.
                row.unaddressed_speech_count = (row.unaddressed_speech_count or 0) + 1
            row.updated_at = now

            envelope = self._envelope_for(row.thread_id, message)
            summary = self._speech_summary(message)

        self._publish(envelope)
        return summary

    # -------- handoff -------------------------------------------------------

    def transfer_owner(
        self,
        *,
        conversation_id: str,
        by_actor: str,
        new_owner: str,
        reason: str | None = None,
    ) -> ConversationSummary:
        envelope: EventEnvelope | None = None
        result: ConversationSummary
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)
            if row.is_general:
                raise ChatConversationStateError(
                    "general conversation has no owner to transfer",
                )
            if row.state == CONVERSATION_STATE_CLOSED:
                raise ChatConversationStateError("conversation is already closed")
            if row.bound_task_id:
                raise ChatConversationStateError(
                    "task-bound conversation owner is governed by the lease; "
                    "release/claim the task instead",
                )
            # Only the current owner or the original opener can hand off.
            # If the conversation has no owner_actor set yet, fall back to
            # opener-only authority.
            authorized_handoff = {row.opener_actor}
            if row.owner_actor:
                authorized_handoff.add(row.owner_actor)
            if by_actor not in authorized_handoff:
                raise ChatConversationStateError(
                    f"by_actor={by_actor!r} is not authorized to hand off; only "
                    f"opener ({row.opener_actor!r}) or current owner "
                    f"({row.owner_actor!r}) may transfer ownership",
                )

            previous_owner = row.owner_actor
            row.owner_actor = new_owner
            row.expected_speaker = new_owner
            now = _utcnow()
            row.updated_at = now

            payload = {
                "conversationId": row.id,
                "previousOwner": previous_owner,
                "newOwner": new_owner,
                "byActor": by_actor,
                "reason": reason,
            }
            event_message = ChatMessageModel(
                thread_id=row.thread_id,
                conversation_id=row.id,
                actor_name=by_actor,
                event_kind=EVENT_CONVERSATION_HANDOFF,
                addressed_to=new_owner,
                content=json.dumps(payload, ensure_ascii=False),
            )
            db.add(event_message)
            db.flush()
            envelope = self._envelope_for(row.thread_id, event_message)
            result = self._summary(row)

        self._publish(envelope)
        return result

    # -------- idle sweep ----------------------------------------------------

    def sweep_idle_conversations(
        self,
        *,
        discord_thread_id: str,
        idle_threshold_seconds: int,
    ) -> list[ConversationSummary]:
        """Multi-tier idle escalation. ``idle_threshold_seconds`` is the
        tier-1 base; tier-2 fires at 4x and tier-3 at 48x.

        Tier 1 (>= 1x base): emit a chat.conversation.idle_warning at level=1.
        Tier 2 (>= 4x base): emit a chat.conversation.idle_warning at level=2.
        Tier 3 (>= 48x base): auto-abandon -- close the conversation with
            resolution="abandoned" via the bypass path. The standard
            chat.conversation.closed event tells observers what happened.

        Each tier fires at most once per conversation (driven by
        ``idle_warning_count`` on the row). A single sweep can advance a
        very-stale conversation through multiple tiers in one call.
        Returns the list of conversations whose tier advanced this call."""

        threshold = max(0, int(idle_threshold_seconds))
        if threshold == 0:
            return []

        flagged: list[ConversationSummary] = []
        envelopes: list[EventEnvelope] = []
        abandon_ids: list[tuple[str, str, dict[str, object]]] = []
        now = _utcnow()

        tier_thresholds = (
            TIER_1_MULTIPLIER * threshold,
            TIER_2_MULTIPLIER * threshold,
            TIER_3_MULTIPLIER * threshold,
        )

        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)

            stmt = (
                select(ChatConversationModel)
                .where(ChatConversationModel.thread_id == thread_row.id)
                .where(ChatConversationModel.state == CONVERSATION_STATE_OPEN)
                .where(ChatConversationModel.is_general.is_(False))
            )
            for row in db.scalars(stmt):
                last_active = row.last_speech_at or row.created_at
                if last_active is None:
                    continue
                last_active_aware = (
                    last_active
                    if last_active.tzinfo is not None
                    else last_active.replace(tzinfo=timezone.utc)
                )
                age_seconds = (now - last_active_aware).total_seconds()

                target_tier = 0
                if age_seconds >= tier_thresholds[2]:
                    target_tier = 3
                elif age_seconds >= tier_thresholds[1]:
                    target_tier = 2
                elif age_seconds >= tier_thresholds[0]:
                    target_tier = 1

                current_tier = row.idle_warning_count or 0
                if target_tier <= current_tier:
                    continue

                # Advance one or more tiers. We emit a warning row for
                # tier-1 / tier-2 transitions; tier-3 is handled by the
                # auto-abandon close after the loop.
                for next_tier in range(current_tier + 1, min(target_tier, 2) + 1):
                    payload = {
                        "conversationId": row.id,
                        "kind": row.kind,
                        "title": row.title,
                        "expectedSpeaker": row.expected_speaker,
                        "ownerActor": row.owner_actor,
                        "lastSpeechAt": last_active_aware.isoformat(),
                        "ageSeconds": int(age_seconds),
                        "idleThresholdSeconds": threshold,
                        "level": next_tier,
                    }
                    event_message = ChatMessageModel(
                        thread_id=row.thread_id,
                        conversation_id=row.id,
                        actor_name="system",
                        event_kind=EVENT_CONVERSATION_IDLE_WARNING,
                        addressed_to=row.expected_speaker,
                        content=json.dumps(payload, ensure_ascii=False),
                    )
                    db.add(event_message)
                    db.flush()
                    envelopes.append(self._envelope_for(row.thread_id, event_message))
                    if row.idle_warning_emitted_at is None:
                        row.idle_warning_emitted_at = now
                    row.idle_warning_count = next_tier

                row.updated_at = now

                if target_tier == 3:
                    abandon_payload = {
                        "ageSeconds": int(age_seconds),
                        "idleThresholdSeconds": threshold,
                    }
                    abandon_ids.append((row.id, last_active_aware.isoformat(), abandon_payload))
                else:
                    flagged.append(self._summary(row))

        # Publish warnings first; abandon-closes go through the public
        # close_conversation path so its own event ordering / publishing
        # stays consistent with the rest of the service.
        for envelope in envelopes:
            self._publish(envelope)

        for conversation_id, last_active_iso, payload in abandon_ids:
            summary = "auto-abandoned: tier-3 idle threshold exceeded"
            self.close_conversation(
                conversation_id=conversation_id,
                closed_by="system",
                resolution="abandoned",
                summary=f"{summary} (age={payload['ageSeconds']}s, base={payload['idleThresholdSeconds']}s, last={last_active_iso})",
                bypass_task_guard=True,
            )
            with session_scope() as db:
                refreshed = db.get(ChatConversationModel, conversation_id)
                if refreshed is not None:
                    refreshed.idle_warning_count = 3
                    flagged.append(self._summary(refreshed))

        return flagged

    # -------- listing / detail ----------------------------------------------

    def list_conversations(
        self,
        *,
        discord_thread_id: str,
        state: str | None = None,
        kind: str | None = None,
        include_general: bool = True,
        limit: int = 50,
    ) -> ConversationListResponse:
        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)

            stmt = select(ChatConversationModel).where(
                ChatConversationModel.thread_id == thread_row.id,
            )
            if state is not None:
                stmt = stmt.where(ChatConversationModel.state == state)
            if kind is not None:
                stmt = stmt.where(ChatConversationModel.kind == kind)
            if not include_general:
                stmt = stmt.where(ChatConversationModel.is_general.is_(False))
            stmt = stmt.order_by(
                ChatConversationModel.is_general.desc(),
                ChatConversationModel.state.asc(),
                ChatConversationModel.updated_at.desc(),
            ).limit(limit)

            rows = list(db.scalars(stmt))
            return ConversationListResponse(
                thread_id=thread_row.id,
                conversations=[self._summary(row) for row in rows],
            )

    def get_conversation(
        self,
        *,
        conversation_id: str,
        recent: int = 30,
    ) -> ConversationDetailResponse:
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)

            messages = list(
                db.scalars(
                    select(ChatMessageModel)
                    .where(ChatMessageModel.conversation_id == row.id)
                    .order_by(ChatMessageModel.created_at.desc())
                    .limit(recent),
                ),
            )
            messages.reverse()
            return ConversationDetailResponse(
                conversation=self._summary(row),
                recent_speech=[
                    self._speech_summary(message) for message in messages
                ],
            )

    # -------- internals -----------------------------------------------------

    @staticmethod
    def _get_thread_row_by_discord(db, discord_thread_id: str) -> ChatThreadModel | None:
        return db.scalar(
            select(ChatThreadModel).where(
                ChatThreadModel.discord_thread_id == discord_thread_id,
            ),
        )

    @staticmethod
    def _get_or_create_general(
        db,
        thread_row: ChatThreadModel,
    ) -> ChatConversationModel:
        return get_or_create_general_conversation(db, thread_row)

    @staticmethod
    def _summary(row: ChatConversationModel) -> ConversationSummary:
        return ConversationSummary(
            id=row.id,
            thread_id=row.thread_id,
            kind=row.kind,
            title=row.title,
            intent=row.intent,
            state=row.state,
            opener_actor=row.opener_actor,
            owner_actor=row.owner_actor,
            expected_speaker=row.expected_speaker,
            parent_conversation_id=row.parent_conversation_id,
            bound_task_id=row.bound_task_id,
            resolution=row.resolution,
            resolution_summary=row.resolution_summary,
            closed_by=row.closed_by,
            is_general=bool(row.is_general),
            last_speech_at=row.last_speech_at,
            speech_count=row.speech_count or 0,
            idle_warning_emitted_at=row.idle_warning_emitted_at,
            idle_warning_count=row.idle_warning_count or 0,
            unaddressed_speech_count=row.unaddressed_speech_count or 0,
            created_at=row.created_at,
            closed_at=row.closed_at,
        )

    @classmethod
    def _summary_payload(cls, row: ChatConversationModel) -> dict[str, Any]:
        summary = cls._summary(row)
        return summary.model_dump(mode="json")

    @staticmethod
    def _speech_summary(message: ChatMessageModel) -> SpeechActSummary:
        kind = message.event_kind
        if kind.startswith("chat.speech."):
            kind = kind[len("chat.speech.") :]
        return SpeechActSummary(
            id=message.id,
            conversation_id=message.conversation_id or "",
            actor_name=message.actor_name,
            kind=kind,
            content=message.content,
            addressed_to=message.addressed_to,
            created_at=message.created_at,
        )

    @staticmethod
    def _envelope_for(thread_id: str, message: ChatMessageModel) -> EventEnvelope:
        return EventEnvelope(
            cursor=encode_event_cursor(
                created_at=message.created_at,
                event_id=message.id,
            ),
            space_id=thread_id,
            event=EventSummary(
                id=message.id,
                kind=message.event_kind,
                actor_name=message.actor_name,
                content=message.content,
                created_at=message.created_at,
            ),
        )

    def _publish(self, envelope: EventEnvelope | None) -> None:
        if envelope is None or self._broker is None:
            return
        self._broker.publish(space_id=envelope.space_id, item=envelope)
