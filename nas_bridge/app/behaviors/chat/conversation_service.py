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

from ...kernel.events import (
    EventEnvelope,
    make_message_envelope,
    publish_envelope,
)
from ...kernel.storage import session_scope
from ...kernel.operation_service import KernelOperationService as RemoteTaskService
from ...kernel.v2 import OperationMirror
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
from .metrics import ChatRoomMetrics
from .models import (
    CONVERSATION_KIND_GENERAL,
    CONVERSATION_KIND_TASK,
    CONVERSATION_STATE_CLOSED,
    CONVERSATION_STATE_OPEN,
    ChatConversationModel,
    ChatConversationReadModel,
    ChatMessageModel,
    ChatMetricSnapshotModel,
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
EVENT_CONVERSATION_OVER_SPEECH = "chat.conversation.over_speech"


# Default threshold at which a non-expected_speaker's chatter trips a
# convergence-pressure warning. Now overridable via ChatPolicyConfig
# (PR19); the constant remains as the global default for back-compat.
OVER_SPEECH_THRESHOLD = 5


# Idle escalation tier multipliers (over the caller-supplied tier-1
# base). With a default 30min tier-1 these resolve to:
#   tier-1: 30min   -> emit chat.conversation.idle_warning level=1
#   tier-2: 2h      -> emit chat.conversation.idle_warning level=2
#   tier-3: 24h     -> auto-abandon (close with resolution=abandoned)
TIER_1_MULTIPLIER = 1
TIER_2_MULTIPLIER = 4
TIER_3_MULTIPLIER = 48


class ChatPolicyConfig:
    """Configurable thresholds for idle escalation + over-speech
    convergence pressure. Default values preserve the
    PR7 / PR-hardening shipping behavior (back-compat). Pass a custom
    instance to ChatConversationService when a deployment / room
    needs different cadence (e.g. incident rooms with 5-min tier-1).
    """

    def __init__(
        self,
        *,
        tier_1_multiplier: int = TIER_1_MULTIPLIER,
        tier_2_multiplier: int = TIER_2_MULTIPLIER,
        tier_3_multiplier: int = TIER_3_MULTIPLIER,
        over_speech_threshold: int = 5,
    ) -> None:
        if not (tier_1_multiplier <= tier_2_multiplier <= tier_3_multiplier):
            raise ValueError(
                f"tier multipliers must be monotonically non-decreasing; got "
                f"{tier_1_multiplier} / {tier_2_multiplier} / {tier_3_multiplier}",
            )
        if over_speech_threshold < 1:
            raise ValueError("over_speech_threshold must be >= 1")
        self.tier_1_multiplier = tier_1_multiplier
        self.tier_2_multiplier = tier_2_multiplier
        self.tier_3_multiplier = tier_3_multiplier
        self.over_speech_threshold = over_speech_threshold


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


def _mirror_v1_message_to_v2(
    db,
    msg: "ChatMessageModel",
    conversation_row: "ChatConversationModel",
    operation_mirror: OperationMirror,
) -> None:
    """F4: Resolve replies_to + addressed_to_many from v1 forms and call
    OperationMirror.mirror_message. Stamps v2_event_id back on the v1
    message row so downstream consumers can join across protocols.

    Module-level so ChatTaskCoordinator and other services that own a
    ChatMessageModel insert can share the dual-write with the same
    semantics ChatConversationService uses.
    """
    if conversation_row.v2_operation_id is None:
        return
    extras: list[str] = []
    if msg.addressed_to_many_json:
        try:
            extras = list(json.loads(msg.addressed_to_many_json) or [])
        except (ValueError, TypeError):
            extras = []
    parent_v2: str | None = None
    if msg.replies_to_speech_id:
        parent = db.get(ChatMessageModel, msg.replies_to_speech_id)
        if parent is not None:
            parent_v2 = parent.v2_event_id
    msg.v2_event_id = operation_mirror.mirror_message(
        db,
        v2_operation_id=conversation_row.v2_operation_id,
        actor_name=msg.actor_name,
        event_kind=msg.event_kind,
        content=msg.content,
        addressed_to=msg.addressed_to,
        addressed_to_many=extras,
        replies_to_v2_event_id=parent_v2,
    )


class ChatConversationNotFoundError(LookupError):
    """Raised when a conversation cannot be located."""


class ChatThreadNotFoundError(LookupError):
    """Raised when the parent chat thread cannot be located."""


class ChatConversationStateError(ValueError):
    """Raised when a transition is not legal for the current conversation state."""


class ChatActorIdentityError(PermissionError):
    """Raised when an actor_authorizer rejects a claimed actor_name.

    Closes failure-mode GAP #06: previously the bridge token only
    authenticated the *caller*, not the *speaker*; mallory could
    submit speech with actor_name='alice'. With an authorizer wired,
    the call is rejected before the row is persisted."""


# Type alias: an actor authorizer takes the caller context (whatever
# the API layer chooses to pass -- typically a token id, BridgeCaller
# instance, or None for un-wired tests) and the actor_name being
# claimed; it returns True if the caller is permitted to speak as
# that actor. Defaulting to "always allow" preserves back-compat for
# call sites that haven't wired identity yet.
ActorAuthorizer = Any  # Callable[[Any, str], bool]; Any to keep the wiring loose


class ChatConversationService:
    def __init__(
        self,
        *,
        subscription_broker: Any | None = None,
        remote_task_service: RemoteTaskService | None = None,
        metrics: ChatRoomMetrics | None = None,
        actor_authorizer: ActorAuthorizer | None = None,
        policy: "ChatPolicyConfig | None" = None,
        operation_mirror: OperationMirror | None = None,
    ) -> None:
        self._broker = subscription_broker
        self._remote_task_service = remote_task_service
        self._metrics = metrics or ChatRoomMetrics()
        # Optional callback (caller_context, actor_name) -> bool. When
        # set, every public method that takes an actor_name validates
        # it against the authorizer first. When unset, all actor_names
        # pass (back-compat with un-wired identity).
        self._actor_authorizer = actor_authorizer
        self._policy = policy or ChatPolicyConfig()
        # Protocol v2 dual-write (F3). Defaults to a real mirror so v2
        # tables fill up automatically; tests that bootstrap their own
        # DB get the mirror writing into v2 alongside v1.
        self._operation_mirror = operation_mirror or OperationMirror()

    def _check_actor(self, actor_name: str, *, caller_context: Any = None) -> None:
        if self._actor_authorizer is None:
            return
        if not self._actor_authorizer(caller_context, actor_name):
            raise ChatActorIdentityError(
                f"caller is not authorized to act as {actor_name!r}"
            )

    @property
    def metrics(self) -> ChatRoomMetrics:
        return self._metrics

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
        caller_context: Any = None,
    ) -> ConversationSummary:
        self._check_actor(request.opener_actor, caller_context=caller_context)
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

            # F3 dual-write: mirror v1 conversation as a v2 Operation in
            # the same transaction. Failures here would taint the v1
            # write; while the mirror is non-authoritative this is the
            # intended coupling -- if v2 writes break we want to know.
            row.v2_operation_id = self._operation_mirror.mirror_conversation_open(
                db,
                v1_conversation_id=row.id,
                thread_id=thread_row.id,
                kind=row.kind,
                title=row.title,
                intent=row.intent,
                opener_actor=row.opener_actor,
                owner_actor=row.owner_actor,
                addressed_to=row.expected_speaker,
                is_general=False,
            )

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
            _mirror_v1_message_to_v2(db, event_message, row, self._operation_mirror)
            envelope = self._envelope_for(thread_row.id, event_message)
            summary = self._summary(row)

        self._metrics.record_conversation_opened()
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
        caller_context: Any = None,
    ) -> ConversationSummary:
        # bypass_task_guard already implies "system-level authority"
        # (auto-abandon by sweep_idle, auto-close by task complete).
        # Skip the actor identity check on those paths -- the closer
        # is "system" or the lease holder, both of which the
        # authorizer wouldn't normally know about.
        if not bypass_task_guard:
            self._check_actor(closed_by, caller_context=caller_context)
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

            # F3 dual-write: mirror the close to v2 in the same tx.
            self._operation_mirror.mirror_conversation_close(
                db,
                v2_operation_id=row.v2_operation_id,
                closed_by_actor=closed_by,
                resolution=resolution,
                resolution_summary=summary,
            )

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
            _mirror_v1_message_to_v2(db, event_message, row, self._operation_mirror)
            envelope = self._envelope_for(row.thread_id, event_message)
            result = self._summary(row)

        self._metrics.record_conversation_closed(resolution=resolution)
        self._publish(envelope)
        return result

    # -------- speech ---------------------------------------------------------

    def submit_speech(
        self,
        *,
        conversation_id: str,
        request: SpeechActSubmitRequest,
        caller_context: Any = None,
    ) -> SpeechActSummary:
        self._check_actor(request.actor_name, caller_context=caller_context)
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
            # PR20 multi-address normalization: the primary slot
            # (``addressed_to``) drives expected_speaker; if the caller
            # only supplied addressed_to_many, lift its first element
            # into the primary slot so existing turn-taking logic keeps
            # working unchanged.
            primary_addr = request.addressed_to
            extras = list(request.addressed_to_many or [])
            if primary_addr is None and extras:
                primary_addr = extras[0]
                extras_excluding_primary = extras[1:]
            else:
                extras_excluding_primary = [a for a in extras if a != primary_addr]
            many_json = (
                json.dumps([primary_addr, *extras_excluding_primary], ensure_ascii=False)
                if (primary_addr and extras_excluding_primary) else
                (
                    json.dumps([primary_addr], ensure_ascii=False) if (primary_addr and extras) else
                    (json.dumps(extras_excluding_primary, ensure_ascii=False) if extras_excluding_primary else None)
                )
            )
            # The primary_addr (possibly lifted from many) is what
            # turn-taking logic below should treat as request.addressed_to.
            effective_addr = primary_addr
            message = ChatMessageModel(
                thread_id=row.thread_id,
                conversation_id=row.id,
                actor_name=request.actor_name,
                event_kind=_speech_event_kind(request.kind),
                addressed_to=primary_addr,
                addressed_to_many_json=many_json,
                replies_to_speech_id=request.replies_to_speech_id,
                content=clean,
            )
            db.add(message)
            db.flush()
            _mirror_v1_message_to_v2(db, message, row, self._operation_mirror)

            row.last_speech_at = now
            row.speech_count = (row.speech_count or 0) + 1

            # Soft turn-taking gauge — count speech that arrives from
            # someone OTHER than the currently-expected_speaker since the
            # last address. Reset to 0 whenever expected_speaker changes
            # (a new address effectively rebases the round). Use
            # ``effective_addr`` so multi-address-only callers (where
            # primary was lifted from addressed_to_many[0]) still drive
            # the slot.
            over_speech_envelope: EventEnvelope | None = None
            if effective_addr:
                if effective_addr != row.expected_speaker:
                    row.unaddressed_speech_count = 0
                row.expected_speaker = effective_addr
            elif request.actor_name == row.expected_speaker:
                # the expected speaker just spoke -- clear the slot and
                # reset the gauge (round resolved).
                row.expected_speaker = None
                row.unaddressed_speech_count = 0
            elif row.expected_speaker:
                # someone other than the expected speaker chimed in
                # without addressing anyone -- bump the noise gauge.
                row.unaddressed_speech_count = (row.unaddressed_speech_count or 0) + 1
                if row.unaddressed_speech_count == self._policy.over_speech_threshold:
                    # Convergence-pressure trip: emit a one-shot
                    # system event so an observer (or auto-handler)
                    # sees the noise. Closes failure-mode GAP #10.
                    over_payload = {
                        "conversationId": row.id,
                        "expectedSpeaker": row.expected_speaker,
                        "unaddressedSpeechCount": row.unaddressed_speech_count,
                        "threshold": self._policy.over_speech_threshold,
                    }
                    over_msg = ChatMessageModel(
                        thread_id=row.thread_id,
                        conversation_id=row.id,
                        actor_name="system",
                        event_kind=EVENT_CONVERSATION_OVER_SPEECH,
                        addressed_to=row.expected_speaker,
                        content=json.dumps(over_payload, ensure_ascii=False),
                    )
                    db.add(over_msg)
                    db.flush()
                    _mirror_v1_message_to_v2(db, over_msg, row, self._operation_mirror)
                    over_speech_envelope = self._envelope_for(row.thread_id, over_msg)
            row.updated_at = now

            envelope = self._envelope_for(row.thread_id, message)
            summary = self._speech_summary(message)

        self._metrics.record_speech(kind=request.kind)
        self._publish(envelope)
        if over_speech_envelope is not None:
            self._publish(over_speech_envelope)
        return summary

    # -------- handoff -------------------------------------------------------

    def transfer_owner(
        self,
        *,
        conversation_id: str,
        by_actor: str,
        new_owner: str,
        reason: str | None = None,
        caller_context: Any = None,
    ) -> ConversationSummary:
        self._check_actor(by_actor, caller_context=caller_context)
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
            _mirror_v1_message_to_v2(db, event_message, row, self._operation_mirror)
            envelope = self._envelope_for(row.thread_id, event_message)
            result = self._summary(row)

        self._metrics.record_handoff()
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
            self._policy.tier_1_multiplier * threshold,
            self._policy.tier_2_multiplier * threshold,
            self._policy.tier_3_multiplier * threshold,
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
                    _mirror_v1_message_to_v2(db, event_message, row, self._operation_mirror)
                    envelopes.append(self._envelope_for(row.thread_id, event_message))
                    if row.idle_warning_emitted_at is None:
                        row.idle_warning_emitted_at = now
                    row.idle_warning_count = next_tier
                    self._metrics.record_idle_warning(tier=next_tier)

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

    # -------- persistent metrics + latency (PR17) -------------------------

    def capture_metric_snapshot(
        self,
        *,
        discord_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the current in-memory metric counters as a row in
        chat_metric_snapshots. When thread_id is None, snapshot is
        global. Operators / cron callers use this to build a time
        series across restarts."""
        thread_uuid = None
        if discord_thread_id is not None:
            with session_scope() as db:
                tr = self._get_thread_row_by_discord(db, discord_thread_id)
                if tr is None:
                    raise ChatThreadNotFoundError(discord_thread_id)
                thread_uuid = tr.id
        snap_json = json.dumps(self._metrics.snapshot(), ensure_ascii=False)
        with session_scope() as db:
            row = ChatMetricSnapshotModel(
                thread_id=thread_uuid,
                snapshot_json=snap_json,
            )
            db.add(row)
            db.flush()
            return {
                "id": row.id,
                "thread_id": thread_uuid,
                "captured_at": row.captured_at,
                "snapshot": json.loads(snap_json),
            }

    def get_metric_history(
        self,
        *,
        discord_thread_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return persisted metric snapshots ordered newest-first."""
        thread_uuid = None
        with session_scope() as db:
            if discord_thread_id is not None:
                tr = self._get_thread_row_by_discord(db, discord_thread_id)
                if tr is None:
                    raise ChatThreadNotFoundError(discord_thread_id)
                thread_uuid = tr.id

            stmt = select(ChatMetricSnapshotModel)
            if thread_uuid is not None:
                stmt = stmt.where(ChatMetricSnapshotModel.thread_id == thread_uuid)
            else:
                stmt = stmt.where(ChatMetricSnapshotModel.thread_id.is_(None))
            if since is not None:
                stmt = stmt.where(ChatMetricSnapshotModel.captured_at >= since)
            stmt = stmt.order_by(ChatMetricSnapshotModel.captured_at.desc()).limit(max(1, min(int(limit), 500)))

            result = []
            for row in db.scalars(stmt):
                try:
                    snap = json.loads(row.snapshot_json)
                except (ValueError, TypeError):
                    snap = {}
                result.append({
                    "id": row.id,
                    "thread_id": row.thread_id,
                    "captured_at": row.captured_at,
                    "snapshot": snap,
                })
            return result

    def compute_latency_stats(
        self,
        *,
        discord_thread_id: str | None = None,
        sample_limit: int = 200,
    ) -> dict[str, Any]:
        """Aggregate latency stats over the last N closed conversations.
        Time-to-close is closed_at minus created_at. Returns per-kind
        + overall avg/min/max in seconds."""
        thread_uuid = None
        with session_scope() as db:
            if discord_thread_id is not None:
                tr = self._get_thread_row_by_discord(db, discord_thread_id)
                if tr is None:
                    raise ChatThreadNotFoundError(discord_thread_id)
                thread_uuid = tr.id

            stmt = (
                select(ChatConversationModel)
                .where(ChatConversationModel.state == CONVERSATION_STATE_CLOSED)
                .where(ChatConversationModel.is_general.is_(False))
            )
            if thread_uuid is not None:
                stmt = stmt.where(ChatConversationModel.thread_id == thread_uuid)
            stmt = stmt.order_by(ChatConversationModel.closed_at.desc()).limit(
                max(1, min(int(sample_limit), 1000))
            )

            samples: list[tuple[str, float]] = []
            for row in db.scalars(stmt):
                if row.closed_at is None or row.created_at is None:
                    continue
                created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
                closed = row.closed_at if row.closed_at.tzinfo else row.closed_at.replace(tzinfo=timezone.utc)
                delta = (closed - created).total_seconds()
                if delta < 0:
                    continue
                samples.append((row.kind, delta))

        by_kind: dict[str, dict[str, float]] = {}
        all_deltas: list[float] = []
        for kind, delta in samples:
            bucket = by_kind.setdefault(kind, {"count": 0, "min": delta, "max": delta, "sum": 0.0})
            bucket["count"] += 1
            bucket["min"] = min(bucket["min"], delta)
            bucket["max"] = max(bucket["max"], delta)
            bucket["sum"] += delta
            all_deltas.append(delta)

        # Convert sum to avg
        for kind, b in by_kind.items():
            b["avg"] = b["sum"] / b["count"] if b["count"] else 0.0
            del b["sum"]

        overall = {}
        if all_deltas:
            overall = {
                "count": float(len(all_deltas)),
                "min": min(all_deltas),
                "max": max(all_deltas),
                "avg": sum(all_deltas) / len(all_deltas),
            }
        return {
            "thread_id": discord_thread_id,
            "sample_size": len(samples),
            "by_kind": by_kind,
            "overall": overall,
        }

    # -------- per-actor read cursor (PR21) ---------------------------------

    def mark_conversation_read(
        self,
        *,
        conversation_id: str,
        actor_name: str,
        speech_id: str | None = None,
        caller_context: Any = None,
    ) -> dict[str, Any]:
        """Update the per-actor read cursor on a conversation. When
        ``speech_id`` is None, marks to the latest persisted speech in
        the conversation (i.e. catch-up). Returns the updated cursor +
        a freshly-computed unread_count (which should be 0 right after
        catch-up but is informative if a new speech raced in)."""
        self._check_actor(actor_name, caller_context=caller_context)
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)

            target_speech_id = speech_id
            target_at = None
            if target_speech_id is None:
                # latest speech (any event_kind) in this conversation
                latest = db.scalar(
                    select(ChatMessageModel)
                    .where(ChatMessageModel.conversation_id == conversation_id)
                    .order_by(ChatMessageModel.created_at.desc())
                    .limit(1)
                )
                if latest is not None:
                    target_speech_id = latest.id
                    target_at = latest.created_at
            else:
                target_msg = db.get(ChatMessageModel, target_speech_id)
                if target_msg is None or target_msg.conversation_id != conversation_id:
                    raise ChatConversationStateError(
                        f"speech_id {speech_id!r} does not belong to conversation",
                    )
                target_at = target_msg.created_at

            cursor_row = db.scalar(
                select(ChatConversationReadModel)
                .where(ChatConversationReadModel.conversation_id == conversation_id)
                .where(ChatConversationReadModel.actor_name == actor_name)
            )
            if cursor_row is None:
                cursor_row = ChatConversationReadModel(
                    conversation_id=conversation_id,
                    actor_name=actor_name,
                    last_read_speech_id=target_speech_id,
                    last_read_at=target_at,
                )
                db.add(cursor_row)
            else:
                cursor_row.last_read_speech_id = target_speech_id
                cursor_row.last_read_at = target_at
            db.flush()

            unread = self._unread_count_inside_session(
                db=db,
                conversation_id=conversation_id,
                cursor_at=target_at,
            )
            return {
                "conversation_id": conversation_id,
                "actor_name": actor_name,
                "last_read_speech_id": target_speech_id,
                "last_read_at": target_at,
                "unread_count": unread,
            }

    def get_conversation_read_status(
        self,
        *,
        conversation_id: str,
        actor_name: str,
    ) -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)
            cursor_row = db.scalar(
                select(ChatConversationReadModel)
                .where(ChatConversationReadModel.conversation_id == conversation_id)
                .where(ChatConversationReadModel.actor_name == actor_name)
            )
            cursor_at = cursor_row.last_read_at if cursor_row else None
            unread = self._unread_count_inside_session(
                db=db,
                conversation_id=conversation_id,
                cursor_at=cursor_at,
            )
            return {
                "conversation_id": conversation_id,
                "actor_name": actor_name,
                "last_read_speech_id": cursor_row.last_read_speech_id if cursor_row else None,
                "last_read_at": cursor_at,
                "unread_count": unread,
            }

    @staticmethod
    def _unread_count_inside_session(
        *,
        db,
        conversation_id: str,
        cursor_at: datetime | None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == conversation_id)
        )
        if cursor_at is not None:
            stmt = stmt.where(ChatMessageModel.created_at > cursor_at)
        return db.scalar(stmt) or 0

    # -------- bulk + audit (PR16) ------------------------------------------

    def bulk_close_conversations(
        self,
        *,
        conversation_ids: list[str],
        closed_by: str,
        resolution: str,
        summary: str | None = None,
        bypass_task_guard: bool = False,
        caller_context: Any = None,
    ) -> dict[str, Any]:
        """Operator bulk close. Each id is closed independently; per-id
        errors are captured rather than aborting the whole call. Auth
        and resolution-vocab checks still apply per id (unless
        bypass_task_guard is True)."""
        if not bypass_task_guard:
            self._check_actor(closed_by, caller_context=caller_context)
        results: list[dict[str, Any]] = []
        succeeded = 0
        for cid in conversation_ids:
            try:
                closed = self.close_conversation(
                    conversation_id=cid,
                    closed_by=closed_by,
                    resolution=resolution,
                    summary=summary,
                    bypass_task_guard=bypass_task_guard,
                    caller_context=caller_context,
                )
                results.append({
                    "conversation_id": cid,
                    "ok": True,
                    "resolution": closed.resolution,
                    "error": None,
                })
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "conversation_id": cid,
                    "ok": False,
                    "resolution": None,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        return {
            "requested": len(conversation_ids),
            "succeeded": succeeded,
            "failed": len(conversation_ids) - succeeded,
            "results": results,
        }

    def search_audit_log(
        self,
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
        actor_name: str | None = None,
        event_kind: str | None = None,
        event_kind_prefix: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Query the chat event log. Every persisted ChatMessageModel
        row -- speech, lifecycle, task events alike -- is searchable.
        Each filter is independent; pass any combination."""
        capped_limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        with session_scope() as db:
            stmt = select(ChatMessageModel)
            if thread_id is not None:
                # discord_thread_id -> internal UUID
                thread_row = self._get_thread_row_by_discord(db, thread_id)
                if thread_row is None:
                    return {"items": [], "has_more": False, "next_cursor": None}
                stmt = stmt.where(ChatMessageModel.thread_id == thread_row.id)
            if conversation_id is not None:
                stmt = stmt.where(ChatMessageModel.conversation_id == conversation_id)
            if actor_name is not None:
                stmt = stmt.where(ChatMessageModel.actor_name == actor_name)
            if event_kind is not None:
                stmt = stmt.where(ChatMessageModel.event_kind == event_kind)
            if event_kind_prefix is not None:
                stmt = stmt.where(ChatMessageModel.event_kind.like(f"{event_kind_prefix}%"))
            if from_at is not None:
                stmt = stmt.where(ChatMessageModel.created_at >= from_at)
            if to_at is not None:
                stmt = stmt.where(ChatMessageModel.created_at <= to_at)
            stmt = stmt.order_by(ChatMessageModel.created_at.desc()).limit(capped_limit + 1).offset(offset)
            rows = list(db.scalars(stmt))
            has_more = len(rows) > capped_limit
            page = rows[:capped_limit]
            items = [
                {
                    "id": r.id,
                    "thread_id": r.thread_id,
                    "conversation_id": r.conversation_id,
                    "actor_name": r.actor_name,
                    "event_kind": r.event_kind,
                    "addressed_to": r.addressed_to,
                    "content": r.content,
                    "created_at": r.created_at,
                }
                for r in page
            ]
            next_cursor = str(offset + capped_limit) if has_more else None
            return {"items": items, "has_more": has_more, "next_cursor": next_cursor}

    # -------- health --------------------------------------------------------

    def get_room_health(
        self,
        *,
        discord_thread_id: str,
        idle_threshold_seconds: int = 30 * 60,
    ) -> dict[str, Any]:
        """Live per-thread health snapshot used by the operator-facing
        ``GET /api/chat/threads/{tid}/health`` endpoint.

        Returns counts derived from the DB at call time plus a
        snapshot of the global in-memory metrics. ``idle_candidates``
        is the number of open non-general conversations whose last
        activity is past the ``idle_threshold_seconds`` mark and have
        not yet been warned at that tier."""
        now = _utcnow()
        cutoff = max(0, int(idle_threshold_seconds))

        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)

            open_rows = list(
                db.scalars(
                    select(ChatConversationModel)
                    .where(ChatConversationModel.thread_id == thread_row.id)
                    .where(ChatConversationModel.state == CONVERSATION_STATE_OPEN)
                )
            )
            non_general = [row for row in open_rows if not row.is_general]
            expected_speakers = sorted({
                row.expected_speaker
                for row in non_general
                if row.expected_speaker
            })
            idle_candidates = 0
            for row in non_general:
                last = row.last_speech_at or row.created_at
                if last is None:
                    continue
                last_aware = (
                    last if last.tzinfo is not None
                    else last.replace(tzinfo=timezone.utc)
                )
                age = (now - last_aware).total_seconds()
                # Already-warned conversations don't count as candidates
                # for the SAME tier; an idle_candidate is one whose age
                # exceeds the threshold AND idle_warning_count is below
                # the corresponding tier (level=1 here).
                if age >= cutoff and (row.idle_warning_count or 0) < 1:
                    idle_candidates += 1
            bound_active = sum(
                1 for row in non_general
                if row.kind == "task" and row.bound_task_id is not None
            )

        return {
            "thread_id": discord_thread_id,
            "open_conversations": len(open_rows),
            "idle_candidates": idle_candidates,
            "expected_speakers": expected_speakers,
            "bound_active_tasks": bound_active,
            "metrics": self._metrics.snapshot(),
        }

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
        kinds: list[str] | None = None,
    ) -> ConversationDetailResponse:
        """Return the conversation summary plus the last ``recent`` events.

        ``kinds`` filters the recent events to only ``event_kind`` values
        in the list (case-sensitive exact match). Useful for clients
        that want, e.g., only ``chat.task.evidence`` rows or only
        ``chat.speech.*`` without lifecycle noise.
        """
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)

            stmt = (
                select(ChatMessageModel)
                .where(ChatMessageModel.conversation_id == row.id)
            )
            if kinds:
                stmt = stmt.where(ChatMessageModel.event_kind.in_(kinds))
            stmt = stmt.order_by(ChatMessageModel.created_at.desc()).limit(recent)
            messages = list(db.scalars(stmt))
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

    def _get_or_create_general(
        self,
        db,
        thread_row: ChatThreadModel,
    ) -> ChatConversationModel:
        existed = db.scalar(
            select(ChatConversationModel)
            .where(ChatConversationModel.thread_id == thread_row.id)
            .where(ChatConversationModel.is_general.is_(True))
            .limit(1),
        ) is not None
        row = get_or_create_general_conversation(db, thread_row)
        if not existed and row.v2_operation_id is None:
            # Newly created general -- mirror to v2 (F3).
            row.v2_operation_id = self._operation_mirror.mirror_conversation_open(
                db,
                v1_conversation_id=row.id,
                thread_id=thread_row.id,
                kind=row.kind,
                title=row.title,
                intent=row.intent,
                opener_actor=row.opener_actor,
                owner_actor=row.owner_actor,
                addressed_to=row.expected_speaker,
                is_general=True,
            )
        return row

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
        many: list[str] = []
        if message.addressed_to_many_json:
            try:
                parsed = json.loads(message.addressed_to_many_json)
                if isinstance(parsed, list):
                    many = [str(item) for item in parsed if item]
            except (ValueError, TypeError):
                many = []
        return SpeechActSummary(
            id=message.id,
            conversation_id=message.conversation_id or "",
            actor_name=message.actor_name,
            kind=kind,
            content=message.content,
            addressed_to=message.addressed_to,
            addressed_to_many=many,
            replies_to_speech_id=message.replies_to_speech_id,
            created_at=message.created_at,
        )

    @staticmethod
    def _envelope_for(thread_id: str, message: ChatMessageModel) -> EventEnvelope:
        return make_message_envelope(space_id=thread_id, message=message)

    def _publish(self, envelope: EventEnvelope | None) -> None:
        publish_envelope(self._broker, envelope)
