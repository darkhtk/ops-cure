"""Protocol test behavior — synthetic agents with personas that drive
the v2 protocol through realistic interaction patterns.

Why this exists:
    The v2 stack has 400+ unit + integration tests, but every one of
    them is a hand-crafted assertion sequence. We need a way to:

      1. Stress-test the protocol with diverse, deterministic actors
         (each persona triggers different code paths)
      2. Drive scenarios end-to-end through the same broker / service
         chain real agents use, not via test scaffolding
      3. Discover emergent gaps -- "two personas spoke and the protocol
         silently dropped this event" -- that hand-written tests miss
         because the test author has to think of the case first

How it works:
    PersonaBrain (extends AgentBrain) -- pluggable behavior with a
        deterministic response policy. 5 personas ship:
          - CuriousJuniorBrain    asks follow-ups
          - SkepticalReviewerBrain objects + demands evidence
          - HelpfulSpecialistBrain answers questions, sometimes whispers
          - DecisiveOperatorBrain  closes ops at appropriate signal
          - SilentObserverBrain    never speaks (idle test fixture)

    ScenarioDriver -- wires personas to AgentRunners against the same
        broker as production. Provides open_inquiry / open_proposal /
        open_task helpers, and process_pending(rounds=N) which drains
        the broker backlog through dispatch repeatedly until quiescent.

    ProtocolObservation -- snapshot returned by snapshot(op_id) with
        event count, kind histogram, redaction stats per actor,
        participants, and state-machine transitions observed.

    ProtocolTestService -- orchestrator for scripted scenarios. Each
        scenario returns an Observation that can be asserted against.

The tests in tests/test_behavior_protocol_test.py exercise each
persona individually and run 4 multi-persona scenarios end-to-end.
"""
from .personas import (  # noqa: F401
    PersonaBrain,
    CuriousJuniorBrain,
    SkepticalReviewerBrain,
    HelpfulSpecialistBrain,
    DecisiveOperatorBrain,
    SilentObserverBrain,
    ALL_PERSONAS,
)
from .adversarial import (  # noqa: F401
    WhisperLeakerBrain,
    RogueCloserBrain,
    LoopHostBrain,
    LeaseSquatterBrain,
    InboxSpammerBrain,
    ALL_ADVERSARIAL,
)
from .load import LoadScenarioRunner, LoadObservation  # noqa: F401
from .race import RaceClaimBrain, EagerReplierBrain, RaceCloseBrain  # noqa: F401
from .driver import (  # noqa: F401
    ScenarioDriver,
    ProtocolObservation,
    PersonaSpec,
)
from .service import ProtocolTestService  # noqa: F401

__all__ = [
    "PersonaBrain",
    "CuriousJuniorBrain",
    "SkepticalReviewerBrain",
    "HelpfulSpecialistBrain",
    "DecisiveOperatorBrain",
    "SilentObserverBrain",
    "ALL_PERSONAS",
    "ScenarioDriver",
    "ProtocolObservation",
    "PersonaSpec",
    "ProtocolTestService",
]
