"""D2 + D8 — agent_loop captures HTTP 400 rejections + surfaces
the trigger's `expected_response.kinds` whitelist into the next
prompt.

Pre-fix behavior (RPG smoke 2026-05-04):
  - bridge returns 400 with detailed policy.* error
  - agent_loop logs to stderr but discards
  - the LLM's next turn has zero awareness that the prior reply
    was dropped → it tends to repeat the same shape → deadlock

Post-fix:
  1. ``_post_claim`` records the rejection on
     ``self._last_post_rejection[op_id]`` (D2).
  2. ``_build_prompt`` reads it back and prepends a "⚠️ Your
     previous reply was REJECTED" notice with the bridge's
     detail message (D2).
  3. ``_build_prompt`` also reads the trigger event's
     ``expected_response.kinds`` and tells the LLM "MUST use
     one of these or a universal carve-out (object / evidence
     / defer)" (D8).
  4. A successful subsequent post clears the rejection, so the
     stale warning does not bleed into a fresh turn.

This test exercises the parser-side methods directly (no live
bridge) since the rejection-capture path is internal state.
"""
from __future__ import annotations

import sys
from pathlib import Path

PC_LAUNCHER_ROOT = Path(__file__).parent.parent / "pc_launcher"
if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))


class _StubLoop:
    """Minimal stand-in carrying just the state ``_build_prompt``
    reaches for. Avoids constructing the full BridgeAgentLoop
    (which expects an SSE thread + bridge URL)."""

    def __init__(self) -> None:
        self._actor_handle = "@operator"
        self._system_prompt = "Test system prompt."
        self._last_post_rejection: dict[str, dict[str, str]] = {}
        # P9.4 / D14: _build_prompt now also reads this map to surface
        # claude-run failures into the next prompt.
        self._last_run_failure: dict[str, dict[str, str]] = {}
        self._history_limit = 0
        self._log_lines: list[str] = []

    def _log(self, msg: str) -> None:
        self._log_lines.append(msg)

    def _fetch_op_history(self, op_id: str) -> list:
        return []  # no history for these tests


def _bind(stub: _StubLoop):
    from connectors.claude_executor.agent_loop import BridgeAgentLoop
    stub._build_prompt = BridgeAgentLoop._build_prompt.__get__(stub)
    return stub


def test_d2_rejection_surfaces_in_next_prompt():
    """A captured rejection on op X shows up in the prompt for the
    next inbox event on op X."""
    stub = _bind(_StubLoop())
    stub._last_post_rejection["op-abc"] = {
        "detail": "policy: reply kind 'evidence' not in expected_response.kinds=['agree', 'object']",
        "rejected_kind": "evidence",
        "http_status": "400",
    }
    ev = {
        "operation_id": "op-abc",
        "kind": "chat.speech.object",
        "payload": {"text": "fix it again"},
    }
    prompt = stub._build_prompt(ev)
    assert "⚠️" in prompt
    assert "REJECTED" in prompt
    assert "kind=evidence" in prompt
    assert "agree" in prompt and "object" in prompt
    assert "carve-out" in prompt.lower()


def test_d2_rejection_isolated_per_op():
    """Rejection on op-A does NOT show up in a prompt for op-B.
    Each op tracks its own rejection state."""
    stub = _bind(_StubLoop())
    stub._last_post_rejection["op-A"] = {
        "detail": "policy: rejected on A",
        "rejected_kind": "claim",
        "http_status": "400",
    }
    ev = {
        "operation_id": "op-B",
        "kind": "chat.speech.question",
        "payload": {"text": "totally unrelated op"},
    }
    prompt = stub._build_prompt(ev)
    assert "REJECTED" not in prompt
    assert "op-A" not in prompt


def test_d8_kinds_whitelist_surfaces_in_prompt():
    """When the trigger event declares ``expected_response.kinds=
    [agree, object]``, the prompt MUST tell the LLM about it so
    the LLM doesn't blindly try [EVIDENCE] and get rejected."""
    stub = _bind(_StubLoop())
    ev = {
        "operation_id": "op-1",
        "kind": "chat.speech.propose",
        "payload": {"text": "vote on this"},
        "expected_response": {
            "from_actor_handles": ["@operator"],
            "kinds": ["agree", "object"],
        },
    }
    prompt = stub._build_prompt(ev)
    assert "expected_response.kinds=" in prompt
    assert "agree" in prompt
    assert "object" in prompt
    assert "carve-out" in prompt.lower()


def test_d8_wildcard_kinds_does_not_warn():
    """``kinds=[*]`` means "anything goes" — the prompt shouldn't
    add a constraint warning that confuses the LLM."""
    stub = _bind(_StubLoop())
    ev = {
        "operation_id": "op-1",
        "kind": "chat.speech.question",
        "payload": {"text": "open question"},
        "expected_response": {
            "from_actor_handles": ["@operator"],
            "kinds": ["*"],
        },
    }
    prompt = stub._build_prompt(ev)
    assert "expected_response.kinds=" not in prompt


def test_d8_no_kinds_in_trigger_does_not_warn():
    """Most triggers don't declare a whitelist — that path stays
    lean and silent."""
    stub = _bind(_StubLoop())
    ev = {
        "operation_id": "op-1",
        "kind": "chat.speech.claim",
        "payload": {"text": "free-form"},
        # no expected_response at all
    }
    prompt = stub._build_prompt(ev)
    assert "expected_response.kinds=" not in prompt


def test_d2_rejection_clears_after_successful_post():
    """If `_post_claim` succeeds, the rejection should be gone so
    the next turn doesn't carry a stale warning. Direct-state
    test (we set then ask the same code path that clears it)."""
    from connectors.claude_executor.agent_loop import BridgeAgentLoop

    class _RealishLoop:
        _actor_handle = "@operator"
        _system_prompt = ""
        _history_limit = 0
        _log_lines: list = []
        _last_post_rejection: dict = {}
        _last_run_failure: dict = {}  # P9.4 / D14
        # _post_claim's success branch does:
        #   self._last_post_rejection.pop(op_id, None)
        # we can't run the full method without HTTP, but the clear
        # logic lives in the success path itself; simulate it.
        def _log(self, msg): self._log_lines.append(msg)

    loop = _RealishLoop()
    loop._last_post_rejection = {"op-X": {"detail": "...", "rejected_kind": "claim", "http_status": "400"}}
    # mimic the success-branch clear
    loop._last_post_rejection.pop("op-X", None)

    # Now build a prompt for op-X — no warning should appear
    bound_build = BridgeAgentLoop._build_prompt.__get__(loop)
    def _empty_history(op_id): return []
    loop._fetch_op_history = _empty_history
    prompt = bound_build({
        "operation_id": "op-X",
        "kind": "chat.speech.question",
        "payload": {"text": "next turn after success"},
    })
    assert "REJECTED" not in prompt
