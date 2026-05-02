"""β4: live LLM scenarios. opt-in via BRIDGE_ANTHROPIC_API_KEY.

ScenarioDriver + 3 ClaudeBrain personas (investigator/reviewer/operator)
run a real inquiry chain. The protocol invariants (no infinite loop,
op state changes coherently) hold regardless of LLM output content.
Cost: ~$0.01-0.02 per run.
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
    from app.behaviors.protocol_test import (
        run_live_inquiry_chain, build_claude_personas,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    db.init_db()
    return locals() | {"db": db}


@pytest.mark.skipif(
    not os.environ.get("BRIDGE_ANTHROPIC_API_KEY"),
    reason="BRIDGE_ANTHROPIC_API_KEY not set; live LLM scenario opt-in",
)
def test_live_inquiry_chain_protocol_invariants(tmp_path, monkeypatch):
    """3 Claude personas run the inquiry chain. Protocol invariants
    hold regardless of model output content:
      - rounds_to_quiesce reaches a finite number
      - api_calls > 0 (the brain WAS asked)
      - op state is one of {open, closed} -- no exotic state drift
    """
    m = _bootstrap(tmp_path, monkeypatch)
    api_key = os.environ["BRIDGE_ANTHROPIC_API_KEY"]
    model = os.environ.get("BRIDGE_AGENT_MODEL", "claude-opus-4-7")
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)

    obs, report = m["run_live_inquiry_chain"](
        chat_service=chat,
        broker=broker,
        api_key=api_key,
        model=model,
        max_rounds=8,  # bound budget
    )
    print(f"\n--- live LLM run ---\nobs={obs}\nreport={report}\n")

    # API was actually called
    assert report.api_calls > 0, (
        "no Claude API calls -- brains never received an envelope"
    )
    # Protocol didn't loop infinitely
    assert obs.rounds_to_quiesce <= 8
    # Op landed in a valid state
    assert obs.final_state in {"open", "closed"}
    # If closed, resolution is one of the inquiry kind's vocab
    if obs.final_state == "closed":
        from app.kernel.v2 import contract
        assert obs.final_resolution in contract.ALLOWED_RESOLUTIONS["inquiry"]


@pytest.mark.skipif(
    not os.environ.get("BRIDGE_ANTHROPIC_API_KEY"),
    reason="BRIDGE_ANTHROPIC_API_KEY not set",
)
def test_live_personas_distinct_actor_handles(tmp_path, monkeypatch):
    """build_claude_personas returns 3 PersonaSpec with distinct
    handles. ScenarioDriver should auto-provision 3 actors."""
    m = _bootstrap(tmp_path, monkeypatch)
    api_key = os.environ["BRIDGE_ANTHROPIC_API_KEY"]
    specs = m["build_claude_personas"](api_key)
    handles = {s.handle for s in specs}
    assert handles == {
        "@claude-investigator", "@claude-reviewer", "@claude-operator",
    }


def test_build_claude_personas_works_without_api_call():
    """Even without making an API call, the spec list must be
    constructable for an offline test of the wiring."""
    from app.behaviors.protocol_test import build_claude_personas
    # Use a fake key -- no API call happens at PersonaSpec creation;
    # only at brain instantiation inside ScenarioDriver. We just inspect
    # the specs themselves.
    specs = build_claude_personas("sk-fake")
    assert len(specs) == 3
    handles = [s.handle for s in specs]
    assert "@claude-investigator" in handles
    assert "@claude-reviewer" in handles
    assert "@claude-operator" in handles
    # Each has a distinct system prompt
    prompts = [s.init_kwargs["system_prompt"] for s in specs]
    assert len(set(prompts)) == 3
