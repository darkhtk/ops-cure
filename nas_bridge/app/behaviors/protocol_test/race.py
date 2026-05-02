"""Race-condition personas and scenarios.

In-process ScenarioDriver dispatches synchronously, so 'concurrency'
here means two personas BOTH react to the same triggering envelope
in the same round. The protocol's invariants (seq monotonicity,
opener-only authority, lease first-come-first-served) decide which
race winner is allowed.

Findings recorded as ProtocolObservation + ActionResult details so
tests can assert what the protocol promises.
"""
from __future__ import annotations

from typing import Any

from .personas import PersonaBrain


class RaceClaimBrain(PersonaBrain):
    """Triggers on first event, attempts task.claim. The runner
    dispatches the action via 'task.claim' which is unknown to the
    runner today (H3 added native claim BUT only via /v2 API; the
    AgentRunner's _execute_action vocabulary is speech.* + close).
    So both brains' actions get recorded as 'unknown action kind' --
    the protocol keeps integrity by NOT having a brain-driven claim
    path. Test asserts this is the current behavior."""
    handle = "@race-claimer"
    description = "tries to seize task lease via brain action"

    def respond(self, event_payload, context):
        if not context.get("event_kind", "").startswith("chat.speech."):
            return None
        if self._bump("attempts") > 1:
            return None
        return [{"action": "task.claim", "lease_seconds": 60}]


class EagerReplierBrain(PersonaBrain):
    """Replies to EVERY claim with its own claim. Two instances both
    reply to the same trigger -> both succeed, distinct seqs. Tests
    that the protocol's seq monotonicity holds when two brains land
    speeches in adjacent rounds.

    Caps at 1 reply per op to prevent runaway bouncing.
    """
    handle = "@eager-replier"
    description = "always replies once with a claim; tests parallel writes"

    def respond(self, event_payload, context):
        if context.get("event_kind") != "chat.speech.claim":
            return None
        op = context.get("operation") or {}
        if self._bump(f"replied:{op.get('id')}") > 1:
            return None
        return [{
            "action": "speech.claim",
            "text": f"reply from {context.get('viewer_actor_handle', '?')}",
        }]


class RaceCloseBrain(PersonaBrain):
    """Triggers on first speech, attempts close. Useful when paired
    with a non-opener variant to verify opener-only authority blocks
    the race at the protocol layer (no need for runtime locks)."""
    handle = "@race-closer"
    description = "tries to close ops; tests opener-only authority"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        if not kind.startswith("chat.speech."):
            return None
        op = context.get("operation") or {}
        if self._bump(f"close:{op.get('id')}") > 1:
            return None
        op_kind = op.get("kind", "")
        resolution = {"inquiry": "answered", "proposal": "accepted",
                      "task": "completed"}.get(op_kind)
        if not resolution:
            return None
        return [{
            "action": "close",
            "resolution": resolution,
            "summary": "race-close attempt",
        }]
