"""ProtocolTestService -- thin orchestrator that runs canned scenarios.

Tests instantiate this and call run_inquiry_scenario() / etc. Each
scenario:
    1. spins up the relevant personas
    2. opens an op + seeds events
    3. drains the broker via ScenarioDriver.process_pending
    4. snapshots and returns ProtocolObservation

Scenarios deliberately stay deterministic: same persona init + same
seed events -> same observation. This is the test-fixture promise.
"""
from __future__ import annotations

from typing import Any

from .driver import ScenarioDriver, PersonaSpec, ProtocolObservation
from .personas import (
    CuriousJuniorBrain,
    SkepticalReviewerBrain,
    HelpfulSpecialistBrain,
    DecisiveOperatorBrain,
    SilentObserverBrain,
)


class ProtocolTestService:
    """Holds shared state if a caller wants to run multiple scenarios
    against the same broker, but most tests use single scenarios so
    this class is mostly a namespace."""

    def __init__(
        self,
        *,
        chat_service,
        broker,
    ) -> None:
        self._chat = chat_service
        self._broker = broker

    def _driver(self, personas: list[PersonaSpec]) -> ScenarioDriver:
        return ScenarioDriver(
            chat_service=self._chat,
            broker=self._broker,
            personas=personas,
        )

    # ---- scenarios -----------------------------------------------------

    def run_inquiry_question_chain(self) -> ProtocolObservation:
        """operator opens the inquiry (so it has authority to close at
        the end), specialist gets addressed, junior is brought in as
        observer. Specialist answers; junior follows up on each claim;
        operator closes once the speech threshold is hit."""
        d = self._driver([
            PersonaSpec(CuriousJuniorBrain),
            PersonaSpec(HelpfulSpecialistBrain),
            PersonaSpec(DecisiveOperatorBrain),
        ])
        thread = d.make_thread(suffix="inquiry-chain")
        op_id = d.open_inquiry(
            opener_handle="@operator",
            addressed_to_handle="@helpful-specialist",
            title="how does the audit endpoint work?",
            discord_thread_id=thread,
            extra_participants=["@curious-junior"],
        )
        # operator seeds the question -- the seed counts toward the
        # operator's own speech threshold, but DecisiveOperatorBrain
        # only counts inbound events (its own dispatch is loop-blocked),
        # so this is fine.
        d.post_speech(
            operation_id=op_id,
            actor_handle="@operator",
            kind="question",
            text="walk me through the audit flow?",
            addressed_to_handle="@helpful-specialist",
        )
        rounds = d.process_pending()
        return d.snapshot(op_id, rounds_used=rounds)

    def run_proposal_objection(self) -> ProtocolObservation:
        """operator opens the proposal (ownership = opener for close
        authority). specialist makes the actual propose speech as a
        seed; reviewer objects; operator closes once threshold hit."""
        d = self._driver([
            PersonaSpec(HelpfulSpecialistBrain),
            PersonaSpec(SkepticalReviewerBrain),
            PersonaSpec(DecisiveOperatorBrain, init_kwargs={"close_threshold": 2}),
        ])
        thread = d.make_thread(suffix="proposal-objection")
        op_id = d.open_proposal(
            opener_handle="@operator",
            addressed_to_handle="@skeptical-reviewer",
            title="adopt new logging library",
            discord_thread_id=thread,
            extra_participants=["@helpful-specialist"],
        )
        d.post_speech(
            operation_id=op_id,
            actor_handle="@helpful-specialist",
            kind="propose",
            text="proposing structlog adoption -- saves 30% on incident time",
        )
        rounds = d.process_pending()
        return d.snapshot(op_id, rounds_used=rounds)

    def run_whisper_redaction(self) -> ProtocolObservation:
        """specialist answers + whispers to operator. junior is in op
        but should not see the whisper."""
        d = self._driver([
            PersonaSpec(CuriousJuniorBrain),
            PersonaSpec(
                # Specialist forced to whisper EVERY answer for this scenario.
                _AlwaysWhisperingSpecialist,
            ),
            PersonaSpec(DecisiveOperatorBrain),
        ])
        thread = d.make_thread(suffix="whisper")
        op_id = d.open_inquiry(
            opener_handle="@operator",
            addressed_to_handle="@helpful-specialist",
            title="what's the prod risk?",
            discord_thread_id=thread,
        )
        d.post_speech(
            operation_id=op_id,
            actor_handle="@operator",
            kind="question",
            text="what's the prod risk?",
            addressed_to_handle="@helpful-specialist",
        )
        rounds = d.process_pending()
        return d.snapshot(op_id, rounds_used=rounds)

    def run_silent_observer_keeps_op_open(self) -> ProtocolObservation:
        """only addressee is the silent observer, who never replies. op
        stays open (no auto-close happens here -- that's idle sweeper's
        job, exercised by S4 use case)."""
        d = self._driver([
            PersonaSpec(SilentObserverBrain),
            PersonaSpec(DecisiveOperatorBrain),
        ])
        thread = d.make_thread(suffix="silent")
        op_id = d.open_inquiry(
            opener_handle="@operator",
            addressed_to_handle="@silent-observer",
            title="anyone there?",
            discord_thread_id=thread,
        )
        d.post_speech(
            operation_id=op_id,
            actor_handle="@operator",
            kind="question",
            text="anyone home?",
            addressed_to_handle="@silent-observer",
        )
        rounds = d.process_pending()
        return d.snapshot(op_id, rounds_used=rounds)


# Helper persona used only by the whisper scenario; specialist that
# whispers EVERY answer (vs default that does every 3rd).
class _AlwaysWhisperingSpecialist(HelpfulSpecialistBrain):
    handle = "@helpful-specialist"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        if kind != "chat.speech.question":
            return None
        text = event_payload.get("text", "")[:60]
        return [
            {"action": "speech.claim", "text": f"answer: based on docs, {text}"},
            {
                "action": "speech.claim",
                "text": "(confidence note: 70% on this)",
                "private_to_actors": ["operator"],
            },
        ]
