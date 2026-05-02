"""protocol_test behavior — persona brains + scenarios.

Tests cover:
  - each persona individually (deterministic response policy)
  - 4 multi-persona scenarios end-to-end through real broker + chat
"""
from __future__ import annotations

import os
import sys

import pytest

from conftest import NAS_BRIDGE_ROOT

# Persona unit tests import the brains directly without going through
# _bootstrap, so seed the minimal env so app.config validates.
os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.behaviors.protocol_test import (
        CuriousJuniorBrain, SkepticalReviewerBrain,
        HelpfulSpecialistBrain, DecisiveOperatorBrain, SilentObserverBrain,
        ProtocolTestService,
    )
    db.init_db()
    return locals() | {"db": db}


# ---- persona unit tests -----------------------------------------------------


def test_curious_junior_asks_followup_to_claim():
    brain = __import__(
        "app.behaviors.protocol_test", fromlist=["CuriousJuniorBrain"]
    ).CuriousJuniorBrain()
    actions = brain.respond(
        {"text": "the patch is ready"},
        {"event_kind": "chat.speech.claim", "operation": {"id": "op-1"}},
    )
    assert actions is not None
    assert actions[0]["action"] == "speech.question"
    assert "clarify" in actions[0]["text"]


def test_curious_junior_caps_at_two_questions_per_op():
    from app.behaviors.protocol_test import CuriousJuniorBrain
    brain = CuriousJuniorBrain()
    ctx = {"event_kind": "chat.speech.claim", "operation": {"id": "op-x"}}
    a1 = brain.respond({"text": "first"}, ctx)
    a2 = brain.respond({"text": "second"}, ctx)
    a3 = brain.respond({"text": "third"}, ctx)
    assert a1 is not None and a2 is not None
    assert a3 is None  # capped


def test_skeptical_reviewer_objects_to_propose():
    from app.behaviors.protocol_test import SkepticalReviewerBrain
    brain = SkepticalReviewerBrain()
    actions = brain.respond(
        {"text": "let's adopt re2"},
        {"event_kind": "chat.speech.propose", "operation": {"id": "p"}},
    )
    assert actions and actions[0]["action"] == "speech.object"


def test_skeptical_reviewer_questions_unsourced_numbers():
    from app.behaviors.protocol_test import SkepticalReviewerBrain
    brain = SkepticalReviewerBrain()
    actions = brain.respond(
        {"text": "this saves 30% on prod"},
        {"event_kind": "chat.speech.claim", "operation": {"id": "p"}},
    )
    assert actions and actions[0]["action"] == "speech.question"
    assert "number" in actions[0]["text"]


def test_skeptical_reviewer_ignores_non_numeric_claim():
    from app.behaviors.protocol_test import SkepticalReviewerBrain
    brain = SkepticalReviewerBrain()
    actions = brain.respond(
        {"text": "I think this is fine"},
        {"event_kind": "chat.speech.claim", "operation": {"id": "p"}},
    )
    assert actions is None


def test_helpful_specialist_answers_and_whispers_every_third():
    from app.behaviors.protocol_test import HelpfulSpecialistBrain
    brain = HelpfulSpecialistBrain()
    ctx = {"event_kind": "chat.speech.question", "operation": {
        "id": "op-1", "participants": [{"actor_id": "x", "role": "opener"}],
    }}
    a1 = brain.respond({"text": "q1"}, ctx)
    a2 = brain.respond({"text": "q2"}, ctx)
    a3 = brain.respond({"text": "q3"}, ctx)
    assert len(a1) == 1
    assert len(a2) == 1
    assert len(a3) == 2  # answer + whisper
    assert a3[1].get("private_to_actors") == ["operator"]


def test_decisive_operator_closes_after_threshold():
    from app.behaviors.protocol_test import DecisiveOperatorBrain
    brain = DecisiveOperatorBrain(close_threshold=3)
    op_ctx = lambda: {  # noqa: E731
        "event_kind": "chat.speech.claim",
        "operation": {"id": "op-1", "kind": "inquiry"},
    }
    assert brain.respond({}, op_ctx()) is None
    assert brain.respond({}, op_ctx()) is None
    actions = brain.respond({}, op_ctx())
    assert actions and actions[0]["action"] == "close"
    assert actions[0]["resolution"] == "answered"  # inquiry vocab
    # subsequent triggers don't re-close
    assert brain.respond({}, op_ctx()) is None


def test_decisive_operator_uses_kind_appropriate_resolution():
    from app.behaviors.protocol_test import DecisiveOperatorBrain
    brain = DecisiveOperatorBrain(close_threshold=2)
    ctx = {"event_kind": "chat.speech.claim",
           "operation": {"id": "op-x", "kind": "proposal"}}
    brain.respond({}, ctx)
    actions = brain.respond({}, ctx)
    assert actions[0]["resolution"] == "accepted"


def test_silent_observer_never_speaks():
    from app.behaviors.protocol_test import SilentObserverBrain
    brain = SilentObserverBrain()
    for kind in [
        "chat.speech.claim", "chat.speech.question", "chat.speech.propose",
        "chat.conversation.opened", "chat.task.evidence",
    ]:
        result = brain.respond({"text": "..."}, {"event_kind": kind})
        assert result is None


# ---- end-to-end scenarios ---------------------------------------------------


def test_inquiry_question_chain_scenario(tmp_path, monkeypatch):
    """junior asks, specialist answers, junior follows up, operator
    closes. Protocol invariants: chain converges, op closed=answered,
    no round cap hit."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    svc = m["ProtocolTestService"](chat_service=chat, broker=broker)
    obs = svc.run_inquiry_question_chain()

    assert obs.final_state == "closed"
    assert obs.final_resolution == "answered"
    assert obs.has_event_kind("chat.speech.question")
    assert obs.has_event_kind("chat.speech.claim")
    assert obs.has_event_kind("chat.conversation.closed")
    # at least 2 speech events (junior question + specialist claim)
    assert obs.speech_count() >= 2
    # quiesced quickly
    assert not obs.hit_round_cap


def test_proposal_objection_scenario(tmp_path, monkeypatch):
    """specialist proposes, reviewer objects, operator decides.
    Verifies speech.propose + speech.object kinds + close=accepted."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    svc = m["ProtocolTestService"](chat_service=chat, broker=broker)
    obs = svc.run_proposal_objection()

    assert obs.final_state == "closed"
    assert obs.final_resolution == "accepted"  # proposal vocab
    assert obs.has_event_kind("chat.speech.propose")
    assert obs.has_event_kind("chat.speech.object")
    # objection should have triggered at least once
    assert obs.event_kind_histogram.get("chat.speech.object", 0) >= 1


def test_whisper_redaction_scenario(tmp_path, monkeypatch):
    """specialist whispers to operator. junior should have non-zero
    redacted_count_by_actor entry. operator should NOT (recipient).
    specialist (speaker) should not (own message)."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    svc = m["ProtocolTestService"](chat_service=chat, broker=broker)
    obs = svc.run_whisper_redaction()

    # Identify actor ids from participant roles -- junior is the opener
    # of a different op, so we need to map handles. Easiest: look up
    # by handle via the V2 repo. The driver did ensure_actor for each
    # persona handle, so we know they exist.
    from app.kernel.v2 import V2Repository
    repo = V2Repository()
    with m["db"].session_scope() as session:
        junior_id = repo.get_actor_by_handle(session, "@curious-junior").id
        operator_id = repo.get_actor_by_handle(session, "@operator").id
        specialist_id = repo.get_actor_by_handle(session, "@helpful-specialist").id

    # A whisper happened? Look at the event_kind_histogram + redaction stats
    # The specialist will have whispered at least once because
    # _AlwaysWhisperingSpecialist always does. So junior should see at
    # least 1 redacted.
    assert obs.redacted_count_by_actor.get(junior_id, 0) >= 1, (
        "junior should miss specialist's whisper to operator; got "
        f"{obs.redacted_count_by_actor}"
    )
    # operator (recipient) and specialist (speaker) should not be redacted
    assert obs.redacted_count_by_actor.get(operator_id, 0) == 0
    assert obs.redacted_count_by_actor.get(specialist_id, 0) == 0


def test_silent_observer_keeps_op_open(tmp_path, monkeypatch):
    """only the silent persona is addressed -- nothing happens after
    operator's question; op stays open until idle sweep (not exercised
    here). Reaches quiescence with op.state=open."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    svc = m["ProtocolTestService"](chat_service=chat, broker=broker)
    obs = svc.run_silent_observer_keeps_op_open()

    assert obs.final_state == "open"
    assert obs.final_resolution is None
    # No reply was made by the silent observer.
    # The only events should be: opened + the seeded question.
    # operator's persona is also in the room but threshold=4 so
    # nothing closes either.
    assert obs.event_kind_histogram.get("chat.speech.question", 0) == 1
    assert obs.event_kind_histogram.get("chat.conversation.closed", 0) == 0
    assert not obs.hit_round_cap  # quiesced (no responses)


def test_scenarios_quiesce_within_round_cap(tmp_path, monkeypatch):
    """Cross-cutting invariant: protocol always converges. If any of
    the scenarios hit the round cap, that's a real bug (loop guard
    failure or persona policy that flaps). Run all 4 and assert."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    svc = m["ProtocolTestService"](chat_service=chat, broker=broker)
    obs_list = [
        svc.run_inquiry_question_chain(),
        svc.run_proposal_objection(),
        svc.run_whisper_redaction(),
        svc.run_silent_observer_keeps_op_open(),
    ]
    for obs in obs_list:
        assert not obs.hit_round_cap, (
            f"scenario op={obs.operation_id} did not quiesce; "
            f"rounds={obs.rounds_to_quiesce} histogram={obs.event_kind_histogram}"
        )
