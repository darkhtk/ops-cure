"""Pre-built persona brains for protocol testing.

Each persona is a deterministic AgentBrain implementation with a
specific behavior trigger. Together they exercise different protocol
paths without needing per-scenario brain authoring.

Determinism: each persona's response depends ONLY on the event_kind +
payload + simple counters tracked on the brain instance. No
randomness, no LLM, no external state. Same input -> same output.
This is essential for protocol regression -- a flaky persona would
mask bugs.
"""
from __future__ import annotations

from typing import Any


class PersonaBrain:
    """Base for the canned personas. Subclasses override ``respond``.

    Tracks `_counters[event_kind]` so subclasses can implement
    "every Nth speech" or "after K replies" policies.
    """

    handle: str = "@unnamed-persona"
    description: str = ""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def _bump(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def respond(
        self,
        event_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        return None


# ---------------------------------------------------------------------------
# CuriousJuniorBrain -- asks follow-up questions on every claim addressed
# at it. Tests speech.question chain, addressed_to flow, reply chain.
# ---------------------------------------------------------------------------
class CuriousJuniorBrain(PersonaBrain):
    handle = "@curious-junior"
    description = "asks follow-ups; tests question chain"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        if kind != "chat.speech.claim":
            return None
        # Junior asks one follow-up per inbound claim, but at most 2 per op
        # (to avoid runaway loops).
        op_id = (context.get("operation") or {}).get("id", "")
        n = self._bump(f"q:{op_id}")
        if n > 2:
            return None
        original = event_payload.get("text", "")[:60]
        return [{
            "action": "speech.question",
            "text": f"could you clarify: {original}?",
        }]


# ---------------------------------------------------------------------------
# SkepticalReviewerBrain -- objects on every propose, demands evidence
# on every claim that mentions a fact (heuristic: contains digit or
# capitalized noun). Tests speech.object kind, vocab variety.
# ---------------------------------------------------------------------------
class SkepticalReviewerBrain(PersonaBrain):
    handle = "@skeptical-reviewer"
    description = "objects to proposals; tests speech.object + vocab"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        text = event_payload.get("text", "")
        if kind == "chat.speech.propose":
            self._bump("objects")
            return [{
                "action": "speech.object",
                "text": f"objection: needs more rigor before adoption",
            }]
        if kind == "chat.speech.claim" and any(c.isdigit() for c in text):
            # Numbers without sources get challenged.
            return [{
                "action": "speech.question",
                "text": "where does that number come from?",
            }]
        return None


# ---------------------------------------------------------------------------
# HelpfulSpecialistBrain -- answers questions with claim, sometimes
# adds an operator-only whisper expressing confidence level. Tests
# claim+whisper combination, private_to_actors path.
# ---------------------------------------------------------------------------
class HelpfulSpecialistBrain(PersonaBrain):
    handle = "@helpful-specialist"
    description = "answers questions; whispers confidence to operator"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        if kind != "chat.speech.question":
            return None
        text = event_payload.get("text", "")[:60]
        actions: list[dict[str, Any]] = [{
            "action": "speech.claim",
            "text": f"answer: based on the docs, {text or '...'}",
        }]
        # Every 3rd answer carries a private confidence note to operator
        # (if @operator participates). Tests whisper redaction.
        n = self._bump("answers")
        if n % 3 == 0:
            participants = (context.get("operation") or {}).get("participants", [])
            # find operator participant if present (we don't know operator's
            # actor_id; we send to handle 'operator' which the runner will
            # resolve to the right actor row).
            actions.append({
                "action": "speech.claim",
                "text": "(confidence note: 70% certain on this one)",
                "private_to_actors": ["operator"],
            })
        return actions


# ---------------------------------------------------------------------------
# DecisiveOperatorBrain -- after observing K speech events on an op,
# closes it with a kind-appropriate resolution. Tests state machine
# close vocab + opener-only authority (operator must be the opener
# for non-bypass close). Per-op counter + threshold.
# ---------------------------------------------------------------------------
class DecisiveOperatorBrain(PersonaBrain):
    handle = "@operator"
    description = "closes ops after enough discussion; tests close vocab"

    def __init__(self, *, close_threshold: int = 4) -> None:
        super().__init__()
        self._close_threshold = close_threshold
        self._closed_ops: set[str] = set()

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        op = context.get("operation") or {}
        op_id = op.get("id")
        if not op_id or op_id in self._closed_ops:
            return None
        # Only count substantive speech (not lifecycle).
        if not kind.startswith("chat.speech."):
            return None
        n = self._bump(f"speech:{op_id}")
        if n < self._close_threshold:
            return None
        # Pick resolution by kind from contract.
        op_kind = op.get("kind", "")
        resolution_by_kind = {
            "inquiry": "answered",
            "proposal": "accepted",
            "task": "completed",
        }
        resolution = resolution_by_kind.get(op_kind)
        if not resolution:
            return None
        self._closed_ops.add(op_id)
        return [{
            "action": "close",
            "resolution": resolution,
            "summary": f"closed by operator after {n} speeches",
        }]


# ---------------------------------------------------------------------------
# SilentObserverBrain -- never speaks. Used for idle escalation testing
# (when only this persona is addressed, the conversation goes idle and
# tier-1/2/3 sweep should fire without another path masking it).
# ---------------------------------------------------------------------------
class SilentObserverBrain(PersonaBrain):
    handle = "@silent-observer"
    description = "never responds; tests idle escalation"

    def respond(self, event_payload, context):
        return None


# Registry for ScenarioDriver to instantiate by handle.
ALL_PERSONAS: tuple[type[PersonaBrain], ...] = (
    CuriousJuniorBrain,
    SkepticalReviewerBrain,
    HelpfulSpecialistBrain,
    DecisiveOperatorBrain,
    SilentObserverBrain,
)
