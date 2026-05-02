"""Chaos personas — brain misbehavior + recovery semantics.

Three chaos modes:
  exception      brain.respond raises every Nth call
  malformed      brain returns garbage action structure
  oversized      brain returns 100+ actions in one batch

For each, the runner should ABSORB the chaos (no propagation up the
loop) AND other brains in the same scenario should keep working.
This is observability + resilience, not correctness -- a chaotic
brain represents a buggy or compromised LLM.
"""
from __future__ import annotations

from typing import Any

from .personas import PersonaBrain


class ChaosExceptionBrain(PersonaBrain):
    """Raises a runtime error from respond(). Tests that the runner's
    try/except absorbs and increments brain_errors counter, leaving
    the rest of the scenario unaffected."""
    handle = "@chaos-exception"
    description = "raises on every speech; tests runner exception handling"

    def respond(self, event_payload, context):
        if not context.get("event_kind", "").startswith("chat.speech."):
            return None
        if self._bump("raises") > 3:
            return None  # cap so test doesn't loop forever
        raise RuntimeError(
            f"chaos-exception #{self._counters['raises']}: this brain is broken"
        )


class ChaosMalformedBrain(PersonaBrain):
    """Returns malformed action dicts -- missing required fields, wrong
    types, etc. Runner's _execute_action should catch and record as
    failed ActionResult."""
    handle = "@chaos-malformed"
    description = "returns garbage actions; tests action validation"

    def respond(self, event_payload, context):
        if not context.get("event_kind", "").startswith("chat.speech."):
            return None
        n = self._bump("returns")
        if n > 3:
            return None
        # Cycle through different malformations
        variants = [
            [{"action": "speech.claim"}],  # missing text
            [{"action": "speech.claim", "text": ""}],  # empty text
            [{"text": "no action key"}],  # missing action
            [{"action": "speech.invalid_kind", "text": "wrong kind"}],
            [{"action": "close"}],  # missing resolution
        ]
        return variants[(n - 1) % len(variants)]


class ChaosOversizedBrain(PersonaBrain):
    """Returns 100 actions in one respond() call. Tests that the
    runner doesn't blow up + that protocol absorbs the burst (some
    succeed, op may move forward). InboxSpammer was burst=5; this
    is burst=100 to find the upper edge."""
    handle = "@chaos-oversized"
    description = "100-action burst; tests rate-limit absence at scale"

    def __init__(self, *, burst: int = 100) -> None:
        super().__init__()
        self._burst = burst

    def respond(self, event_payload, context):
        if not context.get("event_kind", "").startswith("chat.speech."):
            return None
        if self._bump("bursts") > 1:
            return None
        return [
            {"action": "speech.claim", "text": f"oversize-{i}"}
            for i in range(self._burst)
        ]
