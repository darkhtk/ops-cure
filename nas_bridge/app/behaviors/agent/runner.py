"""AgentRunner — broker subscriber that calls a brain on each event."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ...kernel.events import EventEnvelope
from ...kernel.storage import session_scope
from ...kernel.v2 import V2Repository
from ...behaviors.chat.conversation_schemas import SpeechActSubmitRequest
from ...behaviors.chat.models import ChatConversationModel

from .brains import AgentBrain

logger = logging.getLogger("opscure.agent.runner")


@dataclass(frozen=True)
class ActionResult:
    """What the runner did with one brain action. Returned from
    dispatch() so tests can assert."""
    operation_id: str
    action: str
    delivered: bool
    detail: str | None = None


class AgentRunner:
    """One actor, one inbox subscription, one brain. Async-friendly
    but exposes a sync ``dispatch`` so tests don't need an event loop.

    Lifecycle:
        run_forever() : subscribes to broker, loops over next_event,
                        calls dispatch() for each. Stops when stop()
                        is called.
        dispatch(env) : process exactly one envelope. Public for tests
                        and for one-shot scripts.
    """

    def __init__(
        self,
        *,
        actor_handle: str,
        brain: AgentBrain,
        broker,
        chat_service,
        recent_events_limit: int = 20,
        repo: V2Repository | None = None,
    ) -> None:
        self._handle = (
            actor_handle if actor_handle.startswith("@") else f"@{actor_handle}"
        )
        self._brain = brain
        self._broker = broker
        self._chat_service = chat_service
        self._recent_limit = recent_events_limit
        self._repo = repo or V2Repository()
        self._stopping = False
        self._actor_id: str | None = None
        self._subscription = None
        # H5: lightweight per-runner counters surfaced via .metrics.
        # Diagnostic endpoint aggregates these across all runners.
        self._counters: dict[str, int] = {
            "envelopes_seen": 0,
            "skipped_self": 0,
            "skipped_unaddressed": 0,
            "skipped_redacted": 0,
            "brain_invocations": 0,
            "brain_errors": 0,
            "actions_delivered": 0,
            "actions_failed": 0,
        }

    @property
    def metrics(self) -> dict[str, int]:
        """Snapshot of in-process counters. Read-only copy."""
        return dict(self._counters)

    # ---- identity bootstrap ---------------------------------------------

    def _resolve_actor_id(self) -> str:
        if self._actor_id is not None:
            return self._actor_id
        with session_scope() as db:
            actor = self._repo.get_actor_by_handle(db, self._handle)
            if actor is None:
                # First boot: provision via ActorService default capabilities.
                from ...kernel.v2 import ActorService
                actor = ActorService(self._repo).ensure_actor_by_handle(
                    db, handle=self._handle, kind="ai",
                )
            self._actor_id = actor.id
        return self._actor_id

    @property
    def actor_handle(self) -> str:
        return self._handle

    # ---- main loop ------------------------------------------------------

    async def run_forever(self) -> None:
        actor_id = self._resolve_actor_id()
        self._subscription = self._broker.subscribe(
            space_id=f"v2:inbox:{actor_id}",
            subscriber_id=f"agent:{actor_id}",
        )
        try:
            while not self._stopping:
                env = await self._subscription.next_event(timeout_seconds=30.0)
                if env is None:
                    continue  # heartbeat tick
                try:
                    self.dispatch(env)
                except Exception:  # noqa: BLE001 -- never crash the loop
                    logger.exception(
                        "agent runner dispatch failed for envelope %r", env,
                    )
        finally:
            if self._subscription is not None:
                self._subscription.close()
                self._subscription = None

    def stop(self) -> None:
        self._stopping = True

    # ---- one-event handler ----------------------------------------------

    def dispatch(self, envelope: EventEnvelope) -> list[ActionResult]:
        actor_id = self._resolve_actor_id()
        self._counters["envelopes_seen"] += 1
        # Don't react to my own events (loop prevention).
        if envelope.event.actor_name == actor_id:
            self._counters["skipped_self"] += 1
            return []

        try:
            wrapped = json.loads(envelope.event.content)
            if not isinstance(wrapped, dict):
                wrapped = {}
        except (ValueError, TypeError):
            wrapped = {}

        operation_id = wrapped.get("operation_id")
        if not operation_id:
            return []

        # Only respond when we're addressed (or it's a question with no
        # address but we're a participant). Avoids the agent hijacking
        # every speech in a busy room.
        addressed = list(wrapped.get("addressed_to_actor_ids") or [])
        if addressed and actor_id not in addressed:
            self._counters["skipped_unaddressed"] += 1
            return []

        # Privacy: if the event is private and we're not in the recipient
        # list, the broker shouldn't have delivered it -- but defense in
        # depth.
        private_to = wrapped.get("private_to_actor_ids")
        if private_to is not None and actor_id not in private_to:
            self._counters["skipped_redacted"] += 1
            return []

        context = self._build_context(operation_id, envelope)
        # Pass through the incoming event's privacy + addressing meta
        # so brains can react differently to whispers vs. public messages
        # (e.g. WhisperLeakerBrain detects "this came to me as a whisper").
        context["private_to_actor_ids"] = wrapped.get("private_to_actor_ids")
        context["addressed_to_actor_ids"] = wrapped.get("addressed_to_actor_ids") or []
        self._counters["brain_invocations"] += 1
        try:
            actions = self._brain.respond(wrapped.get("payload") or {}, context) or []
        except Exception:  # noqa: BLE001
            self._counters["brain_errors"] += 1
            logger.exception("agent brain raised; treated as no-action")
            return []
        results: list[ActionResult] = []
        for action in actions:
            result = self._execute_action(operation_id, action)
            if result is not None:
                results.append(result)
                if result.delivered:
                    self._counters["actions_delivered"] += 1
                else:
                    self._counters["actions_failed"] += 1
        return results

    # ---- context + action dispatch --------------------------------------

    def _build_context(
        self,
        operation_id: str,
        envelope: EventEnvelope,
    ) -> dict[str, Any]:
        actor_id = self._resolve_actor_id()
        with session_scope() as db:
            op = self._repo.get_operation(db, operation_id)
            if op is None:
                return {"event_kind": envelope.event.kind, "viewer_actor_id": actor_id}
            participants = self._repo.list_participants(db, operation_id=operation_id)
            events = self._repo.list_events(
                db, operation_id=operation_id, limit=self._recent_limit,
            )
            recent = [
                {
                    "seq": e.seq,
                    "kind": e.kind,
                    "actor_id": e.actor_id,
                    "payload": self._repo.event_payload(e),
                }
                for e in events
                # honor whisper redaction at the agent boundary
                if (
                    self._repo.event_private_to(e) is None
                    or actor_id in (self._repo.event_private_to(e) or [])
                    or e.actor_id == actor_id
                )
            ]
            return {
                "event_kind": envelope.event.kind,
                "viewer_actor_id": actor_id,
                "viewer_actor_handle": self._handle,
                "operation": {
                    "id": op.id,
                    "kind": op.kind,
                    "title": op.title,
                    "intent": op.intent,
                    "state": op.state,
                    "participants": [
                        {"actor_id": p.actor_id, "role": p.role}
                        for p in participants
                    ],
                },
                "recent_events": recent,
            }

    def _execute_action(
        self,
        operation_id: str,
        action: dict[str, Any],
    ) -> ActionResult | None:
        kind = (action or {}).get("action")
        if not kind or kind == "ignore":
            return None
        if kind in ("speech.claim", "speech.question", "speech.answer",
                    "speech.propose", "speech.agree", "speech.object",
                    "speech.evidence", "speech.summarize", "speech.react"):
            speech_kind = kind.split(".", 1)[1]
            text = str(action.get("text") or "")
            if not text.strip():
                return ActionResult(operation_id, kind, False, "empty text")
            v1_id = self._operation_id_to_v1_conversation_id(operation_id)
            if v1_id is None:
                return ActionResult(operation_id, kind, False, "no v1 mirror")
            try:
                self._chat_service.submit_speech(
                    conversation_id=v1_id,
                    request=SpeechActSubmitRequest(
                        actor_name=self._handle.lstrip("@"),
                        kind=speech_kind,
                        content=text,
                        addressed_to=action.get("addressed_to"),
                        private_to_actors=action.get("private_to_actors", []) or [],
                    ),
                )
                return ActionResult(operation_id, kind, True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("agent action %s failed", kind)
                return ActionResult(operation_id, kind, False, repr(exc))
        if kind == "close":
            resolution = action.get("resolution")
            if not resolution:
                return ActionResult(operation_id, kind, False, "missing resolution")
            summary = action.get("summary")
            v1_id = self._operation_id_to_v1_conversation_id(operation_id)
            if v1_id is None:
                return ActionResult(operation_id, kind, False, "no v1 mirror")
            try:
                self._chat_service.close_conversation(
                    conversation_id=v1_id,
                    closed_by=self._handle.lstrip("@"),
                    resolution=resolution,
                    summary=summary,
                )
                return ActionResult(operation_id, kind, True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("agent close action failed")
                return ActionResult(operation_id, kind, False, repr(exc))
        # Unknown action -- record and skip.
        return ActionResult(operation_id, kind, False, "unknown action kind")

    def _operation_id_to_v1_conversation_id(self, operation_id: str) -> str | None:
        with session_scope() as db:
            row = db.scalar(
                select(ChatConversationModel)
                .where(ChatConversationModel.v2_operation_id == operation_id)
                .limit(1)
            )
            return row.id if row else None
