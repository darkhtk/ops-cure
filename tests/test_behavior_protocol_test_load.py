"""β1: load scenarios -- N ops × M events through ScenarioDriver.

Quick load (n_ops=10) runs in every regression. Heavy load
(n_ops=100+) is gated behind BRIDGE_RUN_LOAD_TESTS=1.
"""
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
    from app.behaviors.protocol_test import LoadScenarioRunner, LoadObservation
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    db.init_db()
    return locals() | {"db": db}


def test_load_quick_10_ops_quiesces(tmp_path, monkeypatch):
    """sanity: 10 inquiry ops 다 quiesce 도달, all close."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    runner = m["LoadScenarioRunner"](
        chat_service=chat, broker=broker,
        n_ops=10, events_per_op=2, max_rounds=200,
    )
    obs = runner.run_inquiry_load()
    print(f"\n{obs!r}")  # show numbers in test output

    assert obs.n_ops == 10
    assert not obs.hit_round_cap, f"did not quiesce: notes={obs.notes}"
    assert obs.closed_ops >= 1, "no op closed -- chain broken?"
    assert obs.events_per_second > 0


def test_load_quick_does_not_leak_state(tmp_path, monkeypatch):
    """모든 op 가 'closed' 또는 'open' 둘 중 하나. 다른 state 가 있으면
    state machine drift -- bug."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    runner = m["LoadScenarioRunner"](
        chat_service=chat, broker=broker,
        n_ops=10, events_per_op=2,
    )
    obs = runner.run_inquiry_load()
    valid_states = {"open", "closed"}
    extra = set(obs.state_distribution.keys()) - valid_states
    assert not extra, f"unexpected op states present: {extra}"


@pytest.mark.skipif(
    not os.environ.get("BRIDGE_RUN_LOAD_TESTS"),
    reason="BRIDGE_RUN_LOAD_TESTS not set; heavy load runs are opt-in",
)
def test_load_heavy_100_ops(tmp_path, monkeypatch):
    """100 inquiry ops + 평균 ~5 events 각. 회귀 시간 부담 줄이려고
    opt-in. 발견되는 게 있으면 LoadObservation 의 notes / hit_round_cap
    / events_per_second 로 노출."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    runner = m["LoadScenarioRunner"](
        chat_service=chat, broker=broker,
        n_ops=100, events_per_op=3, max_rounds=500,
    )
    obs = runner.run_inquiry_load()
    print(f"\n--- load_heavy_100_ops ---\n{obs!r}\n")
    # Key invariants under load:
    assert obs.n_ops == 100
    assert not obs.hit_round_cap, (
        f"protocol did not quiesce within 500 rounds; notes={obs.notes}"
    )
    # most ops should close (operator brain closes once threshold hit)
    assert obs.closed_ops >= 50, (
        f"only {obs.closed_ops}/100 closed; persona threshold mismatch?"
    )
    # throughput sanity -- if we go below 100 events/sec something is
    # very slow. Numbers refine as we tune; for now record.
    print(f"throughput: {obs.events_per_second:.1f} events/sec")
