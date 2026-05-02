"""β2: race-style scenarios. In-process serial dispatch precludes true
concurrency; what these tests verify is that when TWO brains both
target the same op in the same round, the protocol's invariants
(seq monotonicity, opener-only authority) hold."""
from __future__ import annotations

import os
import sys
import uuid

import pytest

from conftest import NAS_BRIDGE_ROOT

os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))


def _bootstrap(tmp_path, monkeypatch):
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
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.behaviors.protocol_test import (
        ScenarioDriver, PersonaSpec,
        RaceClaimBrain, EagerReplierBrain, RaceCloseBrain,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.kernel.v2 import V2Repository
    db.init_db()
    return locals() | {"db": db}


def test_two_eager_repliers_both_succeed_with_monotonic_seq(tmp_path, monkeypatch):
    """alice 가 question 던지고 두 EagerReplier 가 같이 응답.
    둘 다 succeed, seq 가 strict increasing."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        personas=[
            m["PersonaSpec"](m["EagerReplierBrain"], handle="@eager-1"),
            m["PersonaSpec"](m["EagerReplierBrain"], handle="@eager-2"),
        ],
    )
    thread = d.make_thread(suffix="race-reply")
    op_id = d.open_inquiry(
        opener_handle="@alice",
        addressed_to_handle="@eager-1",
        title="?",
        discord_thread_id=thread,
        extra_participants=["@eager-2"],
    )
    # seed a claim that both eagers react to
    d.post_speech(
        operation_id=op_id, actor_handle="@alice",
        kind="claim", text="kick off",
    )
    d.process_pending()
    obs = d.snapshot(op_id, rounds_used=0)

    # Both brains should have replied (one each cap=1).
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        events = repo.list_events(s, operation_id=op_id, limit=200)
        seqs = [e.seq for e in events]
        # strictly monotonic (no duplicates)
        assert seqs == sorted(set(seqs)) and len(seqs) == len(set(seqs))
        # at least 2 reply claims should exist (one per eager)
        reply_count = sum(
            1 for e in events
            if e.kind == "chat.speech.claim"
            and "reply from" in repo.event_payload(e).get("text", "")
        )
        assert reply_count >= 2, (
            f"expected >=2 eager replies; got {reply_count}, hist={obs.event_kind_histogram}"
        )


def test_race_close_non_opener_blocked_at_authority_layer(tmp_path, monkeypatch):
    """opener=alice. race-closer is participant but not opener.
    race-closer 의 close attempt 가 ChatConversationStateError 로 거부됨.
    op 는 'open' 유지."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        personas=[m["PersonaSpec"](m["RaceCloseBrain"])],
    )
    thread = d.make_thread(suffix="race-close")
    op_id = d.open_inquiry(
        opener_handle="@alice",
        addressed_to_handle="@race-closer",
        title="who can close?",
        discord_thread_id=thread,
    )
    d.post_speech(
        operation_id=op_id, actor_handle="@alice",
        kind="claim", text="poke",
    )
    d.process_pending()
    obs = d.snapshot(op_id, rounds_used=0)

    # race-closer's close attempt must NOT close the op (authority).
    assert obs.final_state == "open", (
        f"non-opener closed op; protocol broken; histogram={obs.event_kind_histogram}"
    )


def test_race_two_closers_both_authorized_first_wins(tmp_path, monkeypatch):
    """opener 가 race-closer 본인일 때 -- 같이 실행해도 한 번만 close
    가능. brain 의 cap (op 당 1회) 보장. 두 번째 close 시도가 있다면
    'already closed' 로 거부됨."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        # spawn TWO race-closers under different handles. Only one is
        # the opener of any given op; the other is a participant.
        personas=[
            m["PersonaSpec"](m["RaceCloseBrain"], handle="@closer-A"),
            m["PersonaSpec"](m["RaceCloseBrain"], handle="@closer-B"),
        ],
    )
    thread = d.make_thread(suffix="race-2close")
    op_id = d.open_inquiry(
        opener_handle="@closer-A",
        addressed_to_handle="@closer-B",
        title="?",
        discord_thread_id=thread,
    )
    d.post_speech(
        operation_id=op_id, actor_handle="@alice",  # any speaker
        kind="claim", text="seed",
    )
    d.process_pending()
    obs = d.snapshot(op_id, rounds_used=0)

    # closer-A is opener -> their close succeeds. closer-B's attempt
    # rejected at authority. Op ends up closed.
    assert obs.final_state == "closed"
    assert obs.final_resolution == "answered"


def test_brain_cannot_emit_task_claim_action(tmp_path, monkeypatch):
    """RaceClaimBrain 의 'task.claim' action 은 runner vocab 에 없음 ->
    'unknown action kind' 로 거부. 결과: 어떤 op state 변경도 없음.
    보호: brain 이 lease 를 직접 잡는 path 가 없어 lease squat impossible."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        personas=[m["PersonaSpec"](m["RaceClaimBrain"])],
    )
    thread = d.make_thread(suffix="race-claim")
    op_id = d.open_inquiry(
        opener_handle="@alice",
        addressed_to_handle="@race-claimer",
        title="?",
        discord_thread_id=thread,
    )
    d.post_speech(
        operation_id=op_id, actor_handle="@alice",
        kind="claim", text="poke",
    )
    d.process_pending()
    obs = d.snapshot(op_id, rounds_used=0)

    # No task.claimed event, no lease state change.
    assert obs.event_kind_histogram.get("chat.task.claimed", 0) == 0
    assert obs.final_state == "open"
