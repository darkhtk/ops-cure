"""Phase 12 (detection layer): progression sweeper.

Detects ops where the last event has a recoverable next-responder
signal (``expected_response``, ``addressed_to``, or
``replies_to_event_id`` author) but no follow-up has arrived within
``idle_s``. Emits a system-authored ``chat.system.nudge`` to the
inferred responder. After ``max_retries`` nudges on the same trigger,
escalates to a ``chat.speech.defer`` so the operator surfaces the
stall.

Design choice — *decision-only* sweeper:

  ``ProgressionSweeper.tick(db)`` returns a list of ``SweepAction``
  records. Production wiring (main.py) actually emits the events
  through the existing chat-service / repo paths. This keeps the
  decision logic unit-testable without spinning up the full bridge,
  and mirrors the policy_sweeper / state_machine "pure validator"
  pattern.

Failure modes covered (phase-12 plan):
- truly TERMINAL events stay silent ("Silence > false invitation"
  preserved)
- closed/abandoned ops are skipped at the recent_active_ops layer
- already-replied responder triggers no nudge (idempotent)
- self-loop (target == last speaker) is rejected
- stale handle / missing actor row → no-op skip
- max_retries cap → escalate to DEFER (no infinite nudge loop)
- nudges themselves don't re-trigger (last.kind must start with
  ``chat.speech.``)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import contract as _contract
from .models import ActorV2Model, OperationEventV2Model
from .repository import V2Repository

logger = logging.getLogger("opscure.progression_sweeper")


@dataclass(frozen=True, slots=True)
class SweepAction:
    op_id: str
    action: str  # "nudge" | "defer" | "skip"
    reason: str
    target_actor_id: str | None = None
    target_handle: str | None = None
    replies_to_event_id: str | None = None


class ProgressionSweeper:
    """Decision-only sweeper. Returns SweepAction list per tick.

    Production wiring is responsible for emitting the actual nudge /
    defer events; this class only decides which ops need them.
    """

    def __init__(
        self,
        *,
        repo: V2Repository | None = None,
        idle_s: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.repo = repo or V2Repository()
        self.idle_s = float(idle_s)
        self.max_retries = int(max_retries)

    def tick(
        self,
        db: Session,
        *,
        now: datetime | None = None,
    ) -> list[SweepAction]:
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.idle_s)
        actions: list[SweepAction] = []
        for op in self.repo.recent_active_ops(db):
            actions.append(self._classify_op(db, op, cutoff))
        return actions

    def _classify_op(self, db: Session, op, cutoff: datetime) -> SweepAction:
        # The trigger is the most-recent SPEECH event. System events
        # (chat.system.nudge, chat.conversation.*, chat.task.*) trail
        # speech turns and must not shadow the real trigger — otherwise
        # a single nudge would short-circuit the whole sweeper to a
        # "last kind not speech" skip on every subsequent tick.
        last = self.repo.last_speech_event_for_op(db, operation_id=op.id)
        if last is None:
            return SweepAction(op.id, "skip", "no speech events")

        # SQLite may round-trip timezone-naive datetimes; coerce.
        last_at = last.created_at
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        if last_at > cutoff:
            return SweepAction(op.id, "skip", "not idle yet")

        expected_response = self.repo.event_expected_response(last)
        addressed = self.repo.event_addressed_to(last)
        replies_to_author_id: str | None = None
        if last.replies_to_event_id:
            prior = db.get(OperationEventV2Model, last.replies_to_event_id)
            if prior is not None:
                replies_to_author_id = prior.actor_id

        inferred = _contract.infer_implicit_responder(
            expected_response=expected_response,
            addressed_actor_ids=addressed,
            replies_to_author_actor_id=replies_to_author_id,
        )
        if inferred is None:
            # Truly TERMINAL — silence is the design intent.
            return SweepAction(op.id, "skip", "terminal")

        channel, value = inferred
        target_actor_id: str | None = None
        target_handle: str | None = None
        if channel == "handle":
            target_handle = value
            actor_row = db.scalar(
                select(ActorV2Model).where(ActorV2Model.handle == value)
            )
            if actor_row is not None:
                target_actor_id = actor_row.id
        elif channel == "actor_id":
            target_actor_id = value
            actor_row = db.get(ActorV2Model, value)
            if actor_row is not None:
                target_handle = actor_row.handle

        if not target_actor_id or not target_handle:
            return SweepAction(
                op.id, "skip",
                "stale or unknown actor for inferred responder",
            )

        # Don't nudge the very actor whose message is the trigger.
        if target_actor_id == last.actor_id:
            return SweepAction(op.id, "skip", "self-loop")

        later_events = self.repo.list_events(
            db, operation_id=op.id, after_seq=last.seq, limit=500,
        )
        if any(e.actor_id == target_actor_id for e in later_events):
            return SweepAction(op.id, "skip", "already replied")

        prior_nudges = sum(
            1 for e in later_events
            if e.kind == _contract.EVENT_SYSTEM_NUDGE
            and e.replies_to_event_id == last.id
        )
        if prior_nudges >= self.max_retries:
            return SweepAction(
                op.id, "defer",
                reason=f"escalating after {prior_nudges} nudges",
                target_actor_id=target_actor_id,
                target_handle=target_handle,
                replies_to_event_id=last.id,
            )

        return SweepAction(
            op.id, "nudge",
            reason=f"idle ≥{self.idle_s}s, channel={channel}",
            target_actor_id=target_actor_id,
            target_handle=target_handle,
            replies_to_event_id=last.id,
        )


class ProgressionRunner:
    """Production wiring around ProgressionSweeper.

    Phase-12 ships the *detection* layer: every tick the runner asks
    the sweeper which ops have a stalled implicit follow-up, and
    structured-logs each action. The emit path (turning a "nudge"
    decision into a real ``chat.system.nudge`` event written through
    the chat-service so v1↔v2 mirroring stays consistent) is phase
    13's job — pulling that into phase 12 doubles the regression
    surface and the ROI is the same.

    Operators who want to act on a stall today can grep the
    ``opscure.progression_sweeper`` log for ``decision=nudge`` /
    ``decision=defer`` and post a manual wake-ping. That's what the
    repo's ``spawn-*-task.ps1`` scripts already do.
    """

    def __init__(
        self,
        *,
        sweeper: ProgressionSweeper,
        session_scope,
        interval_seconds: float = 30.0,
    ) -> None:
        self._sweeper = sweeper
        self._session_scope = session_scope
        self._interval = max(1.0, float(interval_seconds))
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        import asyncio
        logger.info(
            "progression sweeper started: interval=%.1fs idle=%.1fs max_retries=%d",
            self._interval, self._sweeper.idle_s, self._sweeper.max_retries,
        )
        while not self._stopping:
            await asyncio.sleep(self._interval)
            if self._stopping:
                break
            try:
                self._tick_once()
            except Exception:  # noqa: BLE001
                logger.exception("progression sweeper tick failed")

    def _tick_once(self) -> None:
        with self._session_scope() as db:
            actions = self._sweeper.tick(db)
        for a in actions:
            if a.action == "skip":
                continue
            logger.info(
                "progression decision=%s op=%s target=%s replies_to=%s reason=%s",
                a.action, a.op_id, a.target_handle,
                (a.replies_to_event_id or "-")[:8],
                a.reason,
            )
