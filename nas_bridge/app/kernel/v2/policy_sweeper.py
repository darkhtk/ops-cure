"""v3 Phase 2.5 — PolicySweeper.

Background loop that auto-emits ``chat.speech.defer`` from addressees
whose ``expected_response.by_round_seq`` has elapsed without a reply.
Without this, ``by_round_seq`` is a label — declared on the wire,
ignored at runtime.

Design:

- One shared loop running on the bridge's asyncio loop alongside the
  recovery sweeper. Cadence is generous (30s default) because the
  precision required is "did someone fail to answer in time", not
  "real-time".
- Every tick: scan recently-active open ops, walk their event log,
  find triggers with ``by_round_seq`` set, compare against current
  MAX(seq), emit defer on behalf of each non-responding addressee.
- Idempotency: a trigger is considered "deferred" once a speech.defer
  exists with ``replies_to_event_id == trigger.id`` for that addressee.
  Sweeper checks before emitting so duplicate ticks are harmless.
- The emitted defer goes through ``ChatConversationService.submit_speech``
  with ``bypass_actor_authorizer`` — the sweeper is system authority
  speaking *on behalf of* the addressee.

Limits explicitly chosen for this first cut:

- We do NOT attempt to detect "the addressee replied without setting
  replies_to_event_id". External agents now auto-link via agent_loop's
  ``in_reply_to`` parameter. If a non-conformant client posts a reply
  without the link, the sweeper will still emit a defer; the addressee
  thus has two events on the op (their reply + the system defer). This
  is acceptable — the redundancy is harmless and clients are encouraged
  to set replies_to_event_id correctly.
- We do NOT enforce that the auto-defer counts toward ``max_rounds``.
  The defer is itself a chat.speech.defer event so it goes through the
  same insert path; the cap will count it. Tight max_rounds caps may
  prevent a defer from being inserted, in which case the trigger sits
  unresolved — the caller already opted into a tight budget.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from .models import OperationEventV2Model, OperationParticipantV2Model, OperationV2Model
from .repository import V2Repository

if TYPE_CHECKING:
    pass

logger = logging.getLogger("opscure.policy_sweeper")


class PolicySweeper:
    """Periodic sweep of pending ``by_round_seq`` deferrals.

    The sweeper does NOT own a session_scope -- it opens one per tick
    so a long-lived task doesn't pin a SQLite connection.
    """

    def __init__(
        self,
        *,
        chat_service,
        session_scope,  # noqa: ARG002 -- captured at construction
        repo: V2Repository | None = None,
        interval_seconds: float = 30.0,
    ) -> None:
        self._chat = chat_service
        self._session_scope = session_scope
        self._repo = repo or V2Repository()
        self._interval = interval_seconds
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        logger.info(
            "policy sweeper started: interval=%.1fs",
            self._interval,
        )
        while not self._stopping:
            await asyncio.sleep(self._interval)
            if self._stopping:
                break
            try:
                emitted = self._sweep_once()
                if emitted:
                    logger.info("policy sweeper: emitted %d defer(s)", emitted)
            except Exception:  # noqa: BLE001
                logger.exception("policy sweeper tick failed")

    def _sweep_once(self) -> int:
        """One sweep over recently-active open ops. Returns the number
        of speech.defer events emitted this tick. Reads + write phases
        are split into separate session_scopes so the emit path
        (submit_speech) doesn't run inside an open read transaction.
        """
        # Phase 1: read-only scan, collect defers to emit.
        plans: list[tuple[str, str, str]] = []  # (v1_conv_id, handle, trigger_id)
        with self._session_scope() as db:
            open_ops = list(db.scalars(
                select(OperationV2Model).where(OperationV2Model.state != "closed")
            ))
            for op in open_ops:
                plans.extend(self._plan_defers_for_op(db, op))

        # Phase 2: emit each pending defer through submit_speech.
        emitted = 0
        for v1_conv_id, handle, trigger_id in plans:
            if self._emit_defer(
                v1_conv_id=v1_conv_id,
                addressee_handle=handle,
                trigger_event_id=trigger_id,
            ):
                emitted += 1
        return emitted

    def _plan_defers_for_op(
        self, db, op: OperationV2Model,
    ) -> list[tuple[str, str, str]]:
        """Read-only: walk the op's events and return the list of
        (v1_conv_id, addressee_handle, trigger_event_id) tuples that
        need a defer emitted. Caller emits after the read session
        closes."""
        from ...behaviors.chat.models import ChatConversationModel
        v1 = db.execute(
            select(ChatConversationModel).where(
                ChatConversationModel.v2_operation_id == op.id
            )
        ).scalar_one_or_none()
        if v1 is None:
            return []
        v1_conv_id = v1.id
        events = self._repo.list_events(
            db, operation_id=op.id, limit=1000,
        )
        if not events:
            return []
        max_seq = max(e.seq for e in events)

        # Build quick lookups: which (addressee_actor_id, trigger_id)
        # already has a defer? Which addressee has replied directly to
        # which trigger?
        existing_defers: set[tuple[str, str]] = set()
        explicit_replies: set[tuple[str, str]] = set()
        for ev in events:
            if ev.replies_to_event_id is None:
                continue
            if ev.kind == "chat.speech.defer":
                existing_defers.add((ev.actor_id, ev.replies_to_event_id))
            elif ev.kind.startswith("chat.speech."):
                explicit_replies.add((ev.actor_id, ev.replies_to_event_id))

        # Resolve participant handle <-> actor_id once per op so we
        # don't re-query inside the inner loop.
        participants = self._repo.list_participants(db, operation_id=op.id)
        from .models import ActorV2Model
        actor_id_by_handle: dict[str, str] = {}
        if participants:
            actor_rows = db.execute(
                select(ActorV2Model).where(
                    ActorV2Model.id.in_([p.actor_id for p in participants])
                )
            ).scalars().all()
            for actor in actor_rows:
                actor_id_by_handle[actor.handle] = actor.id

        plans: list[tuple[str, str, str]] = []
        for trigger in events:
            ex = self._repo.event_expected_response(trigger)
            if not ex:
                continue
            by = ex.get("by_round_seq")
            if by is None or max_seq < by:
                continue  # window still open
            for handle in ex.get("from_actor_handles") or []:
                addressee_id = actor_id_by_handle.get(handle)
                if not addressee_id:
                    # Addressee never joined the op; the defer would
                    # have nowhere to come from. Skip.
                    continue
                key = (addressee_id, trigger.id)
                if key in existing_defers:
                    continue  # already deferred
                if key in explicit_replies:
                    continue  # they replied
                plans.append((v1_conv_id, handle, trigger.id))
                existing_defers.add(key)  # don't double-emit this tick
        return plans

    def _emit_defer(
        self, *, v1_conv_id: str, addressee_handle: str, trigger_event_id: str,
    ) -> bool:
        """Emit a speech.defer on the addressee's behalf. Returns True
        if it landed. Failures (max_rounds cap reached, op closed mid-
        sweep, etc.) are logged but not raised."""
        from ...behaviors.chat.conversation_schemas import SpeechActSubmitRequest
        try:
            self._chat.submit_speech(
                conversation_id=v1_conv_id,
                request=SpeechActSubmitRequest(
                    actor_name=addressee_handle.lstrip("@"),
                    kind="defer",
                    content=(
                        f"(auto-defer: by_round_seq elapsed without reply from "
                        f"{addressee_handle})"
                    ),
                    replies_to_v2_event_id=trigger_event_id,
                ),
            )
            return True
        except Exception:  # noqa: BLE001 - logged
            logger.exception(
                "policy sweeper: defer emit failed v1=%s addressee=%s trigger=%s",
                v1_conv_id, addressee_handle, trigger_event_id,
            )
            return False
