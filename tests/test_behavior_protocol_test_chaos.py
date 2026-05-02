"""β3: chaos brain scenarios -- runner 흡수 + 다른 brain 진행 보장."""
from __future__ import annotations

import os
import sys

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
    from app.behaviors.protocol_test import (
        ScenarioDriver, PersonaSpec,
        ChaosExceptionBrain, ChaosMalformedBrain, ChaosOversizedBrain,
        HelpfulSpecialistBrain, DecisiveOperatorBrain,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    db.init_db()
    return locals() | {"db": db}


def test_chaos_exception_brain_does_not_crash_runner(tmp_path, monkeypatch):
    """raises every dispatch. Runner catches, brain_errors counter
    bumps, scenario reaches quiescence."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        personas=[m["PersonaSpec"](m["ChaosExceptionBrain"])],
    )
    thread = d.make_thread(suffix="chaos-exc")
    op_id = d.open_inquiry(
        opener_handle="@alice",
        addressed_to_handle="@chaos-exception",
        title="poke",
        discord_thread_id=thread,
    )
    d.post_speech(
        operation_id=op_id, actor_handle="@alice",
        kind="claim", text="poke",
    )
    rounds = d.process_pending()
    obs = d.snapshot(op_id, rounds_used=rounds)

    # Scenario quiesced (didn't loop infinitely)
    assert not obs.hit_round_cap
    # Op state is open (no progress made because brain crashed)
    assert obs.final_state == "open"

    # Runner's brain_errors counter should be > 0
    runners = d.runners_by_handle
    chaos_runner = runners["@chaos-exception"]
    assert chaos_runner.metrics["brain_errors"] >= 1


def test_chaos_does_not_block_other_brains(tmp_path, monkeypatch):
    """chaos-exception 옆에 정상 specialist + operator. specialist 의
    응답은 정상 진행해서 op close 까지 도달해야 한다."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        personas=[
            m["PersonaSpec"](m["ChaosExceptionBrain"]),
            m["PersonaSpec"](m["HelpfulSpecialistBrain"]),
            m["PersonaSpec"](
                m["DecisiveOperatorBrain"],
                init_kwargs={"close_threshold": 1},
            ),
        ],
    )
    thread = d.make_thread(suffix="chaos-mixed")
    op_id = d.open_inquiry(
        opener_handle="@operator",
        addressed_to_handle="@helpful-specialist",
        title="how X?",
        discord_thread_id=thread,
        extra_participants=["@chaos-exception"],
    )
    d.post_speech(
        operation_id=op_id, actor_handle="@operator",
        kind="question", text="how does X work?",
        addressed_to_handle="@helpful-specialist",
    )
    d.process_pending()
    obs = d.snapshot(op_id, rounds_used=0)

    # Despite chaos brain crashing every dispatch, specialist responded
    # and operator closed after threshold.
    assert obs.final_state == "closed", (
        f"chaos blocked progress; histogram={obs.event_kind_histogram}"
    )
    assert obs.final_resolution == "answered"

    # chaos brain still recorded errors
    chaos_runner = d.runners_by_handle["@chaos-exception"]
    assert chaos_runner.metrics["brain_errors"] >= 1


def test_chaos_malformed_actions_recorded_as_failed(tmp_path, monkeypatch):
    """malformed brain returns garbage. Each variant fails action
    dispatch (delivered=False) for a different reason. ActionResult.detail
    captures the reason for diagnostics."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        personas=[m["PersonaSpec"](m["ChaosMalformedBrain"])],
    )
    thread = d.make_thread(suffix="chaos-bad")
    op_id = d.open_inquiry(
        opener_handle="@alice",
        addressed_to_handle="@chaos-malformed",
        title="poke",
        discord_thread_id=thread,
    )
    d.post_speech(
        operation_id=op_id, actor_handle="@alice",
        kind="claim", text="poke",
    )
    d.process_pending()
    obs = d.snapshot(op_id, rounds_used=0)

    # Op stayed open (none of the malformed actions land as real speech)
    assert obs.final_state == "open"
    # actions_delivered should be 0 across the chaos brain's runner
    runner = d.runners_by_handle["@chaos-malformed"]
    assert runner.metrics["actions_delivered"] == 0


def test_chaos_oversized_burst_lands_protocol_absorbs(tmp_path, monkeypatch):
    """100-action burst from one brain. Runner dispatches all
    sequentially. Each successful dispatch becomes a v2 event;
    seq UNIQUE constraint never trips."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    d = m["ScenarioDriver"](
        chat_service=chat, broker=broker,
        personas=[
            m["PersonaSpec"](
                m["ChaosOversizedBrain"], init_kwargs={"burst": 25},
            ),
        ],
        max_rounds=200,  # burst keeps re-feeding broker
    )
    thread = d.make_thread(suffix="chaos-burst")
    op_id = d.open_inquiry(
        opener_handle="@alice",
        addressed_to_handle="@chaos-oversized",
        title="poke",
        discord_thread_id=thread,
    )
    d.post_speech(
        operation_id=op_id, actor_handle="@alice",
        kind="claim", text="trigger",
    )
    d.process_pending()
    obs = d.snapshot(op_id, rounds_used=0)

    # All 25 burst claims should have landed.
    burst_count = sum(
        1 for kind, n in obs.event_kind_histogram.items()
        if kind == "chat.speech.claim"
        for _ in range(n)
    )
    # alice's seed (1) + chaos-oversized's burst (25) + possibly more
    # if junior/specialist chains -- but we have only chaos persona,
    # so 26 minimum.
    assert burst_count >= 26, (
        f"oversized burst didn't fully land; got {burst_count}, "
        f"hist={obs.event_kind_histogram}"
    )
    # quiesced
    assert not obs.hit_round_cap
