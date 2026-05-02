"""ScenarioDriver -- wires personas to runners + drains broker until
quiescent + snapshots invariants per op.

Drives synchronously via AgentRunner.dispatch; no asyncio loop needed.
Each round:
    1. enumerate every envelope sitting in the broker backlog
    2. dispatch un-seen envelopes to the runner whose actor owns the space
    3. brain decisions trigger new chat_service writes -> broker fanout
       lands new envelopes for next round
    4. stop when no new envelope appeared

Quiescence is the key invariant: a healthy protocol always reaches it
eventually. A scenario that doesn't quiesce within max_rounds means
either an infinite loop (loop guards failed) or a flapping persona.
The driver caps rounds and reports if it hit the cap.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ...behaviors.chat.conversation_service import ChatConversationService
from ...behaviors.chat.conversation_schemas import (
    ConversationOpenRequest,
    SpeechActSubmitRequest,
)
from ...behaviors.chat.models import ChatThreadModel, ChatConversationModel
from ...kernel.storage import session_scope
from ...kernel.subscriptions import InProcessSubscriptionBroker
from ...kernel.v2 import V2Repository, ActorService
from ...behaviors.agent.runner import AgentRunner

from .personas import PersonaBrain


@dataclass
class PersonaSpec:
    """One persona to spawn in a scenario. Handle defaults to whatever
    the persona class advertises but can be overridden per-test
    (useful for parameterized scenarios)."""
    persona_cls: type[PersonaBrain]
    handle: str | None = None
    init_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProtocolObservation:
    """Snapshot of one operation after a scenario quiesced."""
    operation_id: str
    final_state: str
    final_resolution: str | None
    event_count: int
    event_kind_histogram: dict[str, int]
    participant_roles: dict[str, list[str]]   # actor_id -> [roles]
    redacted_count_by_actor: dict[str, int]   # actor_id -> num whispers hidden
    rounds_to_quiesce: int
    hit_round_cap: bool

    def has_event_kind(self, kind: str) -> bool:
        return self.event_kind_histogram.get(kind, 0) > 0

    def speech_count(self) -> int:
        return sum(
            v for k, v in self.event_kind_histogram.items()
            if k.startswith("chat.speech.")
        )


class ScenarioDriver:
    """One driver instance per scenario. Owns the broker + chat_service
    + a set of AgentRunners. Each test creates a fresh driver."""

    def __init__(
        self,
        *,
        chat_service: ChatConversationService,
        broker: InProcessSubscriptionBroker,
        personas: list[PersonaSpec],
        repo: V2Repository | None = None,
        max_rounds: int = 20,
    ) -> None:
        self._chat = chat_service
        self._broker = broker
        self._repo = repo or V2Repository()
        self._max_rounds = max_rounds
        self._runners: dict[str, AgentRunner] = {}     # actor_id -> runner
        self._brain_by_handle: dict[str, PersonaBrain] = {}
        self._handle_by_actor_id: dict[str, str] = {}  # actor_id -> handle
        # (space_id, event_id) -- the SAME v2 event fans out to every
        # participant's space, so dedup by event_id alone would skip
        # all but the first actor. Dedup at the (recipient, event)
        # level instead.
        self._dispatched_event_ids: set[tuple[str, str]] = set()
        for spec in personas:
            self._spawn_persona(spec)

    # ---- setup ----------------------------------------------------------

    def _spawn_persona(self, spec: PersonaSpec) -> None:
        handle = spec.handle or spec.persona_cls.handle
        brain = spec.persona_cls(**spec.init_kwargs)
        actor_id = self._ensure_actor(handle)
        runner = AgentRunner(
            actor_handle=handle,
            brain=brain,
            broker=self._broker,
            chat_service=self._chat,
            repo=self._repo,
        )
        self._runners[actor_id] = runner
        self._brain_by_handle[handle] = brain
        self._handle_by_actor_id[actor_id] = handle

    def _ensure_actor(self, handle: str) -> str:
        actor_service = ActorService(self._repo)
        with session_scope() as db:
            actor = actor_service.ensure_actor_by_handle(
                db,
                handle=handle if handle.startswith("@") else f"@{handle}",
            )
            return actor.id

    @property
    def runners_by_handle(self) -> dict[str, AgentRunner]:
        return {h: self._runners[a] for a, h in self._handle_by_actor_id.items()}

    # ---- scenario openers -----------------------------------------------

    def make_thread(self, *, suffix: str = "test") -> str:
        """Create a chat thread + its general conversation. Returns
        discord_thread_id which scenario openers want."""
        import uuid
        with session_scope() as db:
            row = ChatThreadModel(
                id=str(uuid.uuid4()),
                guild_id="g", parent_channel_id="p",
                discord_thread_id=f"d-{suffix}",
                title=f"t-{suffix}",
                created_by="operator",
            )
            db.add(row); db.flush()
            discord = row.discord_thread_id
        # ensure_general avoids the open_conversation lock-vs-task path
        # we hit in earlier scenarios; harmless for inquiry/proposal.
        self._chat.ensure_general(discord_thread_id=discord)
        return discord

    def open_inquiry(
        self,
        *,
        opener_handle: str,
        addressed_to_handle: str | None,
        title: str,
        discord_thread_id: str,
        extra_participants: list[str] | None = None,
    ) -> str:
        """Returns the v2 operation_id.

        ``extra_participants`` adds additional actor handles as
        ``observer`` role on the v2 op directly via repo, so they
        receive broker fanout. Useful for driving personas who are
        meant to oversee but aren't the addressed party (e.g. a
        DecisiveOperatorBrain that closes after watching).
        """
        op_id = self._open(
            kind="inquiry",
            opener_handle=opener_handle,
            addressed_to_handle=addressed_to_handle,
            title=title,
            discord_thread_id=discord_thread_id,
        )
        if extra_participants:
            self._add_extra_participants(op_id, extra_participants)
        return op_id

    def open_proposal(
        self,
        *,
        opener_handle: str,
        addressed_to_handle: str | None,
        title: str,
        discord_thread_id: str,
        extra_participants: list[str] | None = None,
    ) -> str:
        op_id = self._open(
            kind="proposal",
            opener_handle=opener_handle,
            addressed_to_handle=addressed_to_handle,
            title=title,
            discord_thread_id=discord_thread_id,
        )
        if extra_participants:
            self._add_extra_participants(op_id, extra_participants)
        return op_id

    def _add_extra_participants(self, op_id: str, handles: list[str]) -> None:
        actor_service = ActorService(self._repo)
        with session_scope() as db:
            existing = {
                p.actor_id
                for p in self._repo.list_participants(db, operation_id=op_id)
            }
            for handle in handles:
                normalized = handle if handle.startswith("@") else f"@{handle}"
                actor = actor_service.ensure_actor_by_handle(db, handle=normalized)
                if actor.id in existing:
                    continue
                self._repo.add_participant(
                    db, operation_id=op_id, actor_id=actor.id, role="observer",
                )

    def _open(
        self,
        *,
        kind: str,
        opener_handle: str,
        addressed_to_handle: str | None,
        title: str,
        discord_thread_id: str,
    ) -> str:
        opener_actor = opener_handle.lstrip("@")
        addressed = addressed_to_handle.lstrip("@") if addressed_to_handle else None
        summary = self._chat.open_conversation(
            discord_thread_id=discord_thread_id,
            request=ConversationOpenRequest(
                kind=kind, title=title,
                opener_actor=opener_actor,
                addressed_to=addressed,
            ),
        )
        with session_scope() as db:
            v1 = db.get(ChatConversationModel, summary.id)
            return v1.v2_operation_id

    def post_speech(
        self,
        *,
        operation_id: str,
        actor_handle: str,
        kind: str = "claim",
        text: str,
        addressed_to_handle: str | None = None,
        private_to_handles: list[str] | None = None,
    ) -> None:
        """Inject a synthetic speech as ``actor_handle``. Used to seed
        scenarios."""
        v1_id = self._operation_id_to_v1(operation_id)
        if v1_id is None:
            raise RuntimeError(f"no v1 mirror for op {operation_id}")
        priv = [h.lstrip("@") for h in (private_to_handles or [])]
        addr = addressed_to_handle.lstrip("@") if addressed_to_handle else None
        self._chat.submit_speech(
            conversation_id=v1_id,
            request=SpeechActSubmitRequest(
                actor_name=actor_handle.lstrip("@"),
                kind=kind, content=text,
                addressed_to=addr,
                private_to_actors=priv,
            ),
        )

    def _operation_id_to_v1(self, operation_id: str) -> str | None:
        with session_scope() as db:
            row = db.scalar(
                select(ChatConversationModel)
                .where(ChatConversationModel.v2_operation_id == operation_id)
                .limit(1)
            )
            return row.id if row else None

    # ---- the loop -------------------------------------------------------

    def process_pending(self, *, max_rounds: int | None = None) -> int:
        """Dispatch every undispatched envelope through the appropriate
        runner. Repeat until quiescent (no new envelopes appear) or
        max_rounds reached. Returns the round count actually used."""
        cap = max_rounds if max_rounds is not None else self._max_rounds
        rounds = 0
        for _ in range(cap):
            rounds += 1
            backlog_snapshot = list(self._broker._backlog.items())
            new_dispatched = 0
            for space_id, queue in backlog_snapshot:
                if not space_id.startswith("v2:inbox:"):
                    continue
                actor_id = space_id[len("v2:inbox:"):]
                runner = self._runners.get(actor_id)
                if runner is None:
                    continue
                for envelope in list(queue):
                    key = (space_id, envelope.event.id)
                    if key in self._dispatched_event_ids:
                        continue
                    self._dispatched_event_ids.add(key)
                    new_dispatched += 1
                    try:
                        runner.dispatch(envelope)
                    except Exception:  # noqa: BLE001
                        # Log but don't crash the driver; persona tests
                        # are diagnostics, an exception is itself a
                        # finding.
                        import logging
                        logging.getLogger("opscure.protocol_test").exception(
                            "dispatch failed for envelope %s", envelope.event.id,
                        )
            # Quiescent if nothing new came in AND no further dispatch.
            if new_dispatched == 0:
                break
        return rounds

    # ---- snapshot -------------------------------------------------------

    def snapshot(self, operation_id: str, *, rounds_used: int = 0) -> ProtocolObservation:
        """Build observation of an op's final state."""
        repo = self._repo
        with session_scope() as db:
            op = repo.get_operation(db, operation_id)
            if op is None:
                raise RuntimeError(f"snapshot: op {operation_id} missing")
            events = repo.list_events(db, operation_id=operation_id, limit=1000)
            participants = repo.list_participants(db, operation_id=operation_id)
            histogram: dict[str, int] = {}
            for ev in events:
                histogram[ev.kind] = histogram.get(ev.kind, 0) + 1
            participant_roles: dict[str, list[str]] = {}
            for p in participants:
                participant_roles.setdefault(p.actor_id, []).append(p.role)

            # redaction stats: per actor, how many private events would
            # be invisible. (Independent of who spoke them.)
            redacted: dict[str, int] = {a: 0 for a in self._handle_by_actor_id}
            for ev in events:
                priv = repo.event_private_to(ev)
                if priv is None:
                    continue
                for actor_id in self._handle_by_actor_id:
                    if actor_id == ev.actor_id:
                        continue  # speaker sees own
                    if actor_id in priv:
                        continue  # recipient sees
                    redacted[actor_id] = redacted.get(actor_id, 0) + 1

            return ProtocolObservation(
                operation_id=operation_id,
                final_state=op.state,
                final_resolution=op.resolution,
                event_count=len(events),
                event_kind_histogram=histogram,
                participant_roles=participant_roles,
                redacted_count_by_actor=redacted,
                rounds_to_quiesce=rounds_used,
                hit_round_cap=(
                    rounds_used >= self._max_rounds
                ),
            )
