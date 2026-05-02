"""H4: ClaudeBrain tool use parsing + (opt-in) live Anthropic call."""
from __future__ import annotations

import os
import sys

import pytest

from conftest import NAS_BRIDGE_ROOT

os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))


# ---- pure unit tests of tool definition + parser (no SDK) -----------------


def test_tools_built_from_contract_speech_kinds():
    """Every SPEECH_KINDS value -> one speech_<kind> tool. + close_operation."""
    from app.behaviors.agent import _build_claude_tools
    from app.kernel.v2 import contract

    tools = _build_claude_tools()
    names = {t["name"] for t in tools}
    expected_speech = {f"speech_{k}" for k in contract.SPEECH_KINDS}
    assert expected_speech.issubset(names)
    assert "close_operation" in names
    # exactly the right cardinality
    assert len(tools) == len(contract.SPEECH_KINDS) + 1


def test_tool_use_parser_speech_claim_with_text():
    from app.behaviors.agent import _tool_uses_to_actions
    actions = _tool_uses_to_actions([{
        "name": "speech_claim",
        "input": {"text": "the build is broken"},
    }])
    assert actions == [{"action": "speech.claim", "text": "the build is broken"}]


def test_tool_use_parser_speech_with_addressed_to_and_private():
    from app.behaviors.agent import _tool_uses_to_actions
    actions = _tool_uses_to_actions([{
        "name": "speech_claim",
        "input": {
            "text": "psst",
            "addressed_to": "operator",
            "private_to_actors": ["operator"],
        },
    }])
    assert actions[0]["text"] == "psst"
    assert actions[0]["addressed_to"] == "operator"
    assert actions[0]["private_to_actors"] == ["operator"]


def test_tool_use_parser_close_operation():
    from app.behaviors.agent import _tool_uses_to_actions
    actions = _tool_uses_to_actions([{
        "name": "close_operation",
        "input": {"resolution": "answered", "summary": "done"},
    }])
    assert actions == [{
        "action": "close", "resolution": "answered", "summary": "done",
    }]


def test_tool_use_parser_skips_empty_text():
    from app.behaviors.agent import _tool_uses_to_actions
    assert _tool_uses_to_actions([
        {"name": "speech_claim", "input": {"text": ""}},
        {"name": "speech_claim", "input": {"text": "   "}},
    ]) == []


def test_tool_use_parser_skips_unknown_tool():
    from app.behaviors.agent import _tool_uses_to_actions
    assert _tool_uses_to_actions([
        {"name": "do_the_dance", "input": {"text": "wrong"}},
    ]) == []


def test_tool_use_parser_handles_missing_resolution():
    from app.behaviors.agent import _tool_uses_to_actions
    assert _tool_uses_to_actions([
        {"name": "close_operation", "input": {"summary": "no resolution given"}},
    ]) == []


def test_claude_brain_instantiation_without_api_key_still_loads_module():
    """Sanity: importing brains module doesn't trigger anthropic SDK."""
    from app.behaviors.agent.brains import ClaudeBrain  # noqa: F401
    # We don't construct it here -- that needs api_key. Just verify
    # the soft-import pattern lets the module load.


# ---- opt-in live test (skipped unless BRIDGE_ANTHROPIC_API_KEY set) ----


@pytest.mark.skipif(
    not os.environ.get("BRIDGE_ANTHROPIC_API_KEY"),
    reason="needs BRIDGE_ANTHROPIC_API_KEY set; live Anthropic call",
)
def test_claude_brain_live_round_trip():
    """End-to-end live test. Costs ~$0.01 per run.

    Sends a simple inquiry-style trigger and asserts the brain returns
    a valid speech.* action with non-empty text. Doesn't validate the
    *content* of the answer -- just that the contract (tool selection +
    parser) works against the live API."""
    from app.behaviors.agent import ClaudeBrain
    api_key = os.environ["BRIDGE_ANTHROPIC_API_KEY"]
    model = os.environ.get("BRIDGE_AGENT_MODEL", "claude-opus-4-7")
    brain = ClaudeBrain(api_key=api_key, model=model, max_tokens=200)
    actions = brain.respond(
        {"text": "what is 2 plus 2?"},
        {
            "event_kind": "chat.speech.question",
            "viewer_actor_id": "test-actor-id",
            "viewer_actor_handle": "@test-bot",
            "operation": {
                "id": "op-test", "kind": "inquiry",
                "title": "math test", "intent": "verify arithmetic",
                "state": "open", "participants": [],
            },
            "recent_events": [],
        },
    )
    assert actions, "brain produced no action"
    assert len(actions) >= 1
    first = actions[0]
    assert first["action"].startswith("speech."), first
    assert first.get("text", "").strip(), "empty reply text"
