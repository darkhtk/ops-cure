"""F10: per-kind state machine for v2 Operations.

A single ``OperationStateMachine`` validates whether a (kind,
current_state, target_state, resolution?) transition is legal.
Used at the boundary just before close_operation / reopen / kind-
specific transitions land.

Operation kinds and their state graphs:

  general     : open <-> open               (never closes; close attempts rejected)

  inquiry     : open -> closed[answered]
                open -> closed[redirected]
                open -> closed[abandoned]

  proposal    : open -> closed[accepted]
                open -> closed[rejected]
                open -> closed[withdrawn]
                open -> closed[abandoned]

  task        : open -> claimed -> executing -> verifying -> closed[completed]
                claimed -> executing -> closed[failed]
                executing -> blocked_approval -> executing -> ...
                <any non-terminal> -> closed[abandoned]   (system bypass)

The task state graph is intentionally a *superset* of v1's
RemoteTaskService statuses; the canonical task substates still live
in remote_tasks. This module governs only the v2 ``state`` column on
operations_v2 and the ``resolution`` final value.
"""
from __future__ import annotations

from dataclasses import dataclass, field

KIND_GENERAL = "general"
KIND_INQUIRY = "inquiry"
KIND_PROPOSAL = "proposal"
KIND_TASK = "task"

STATE_OPEN = "open"
STATE_CLAIMED = "claimed"
STATE_EXECUTING = "executing"
STATE_BLOCKED_APPROVAL = "blocked_approval"
STATE_VERIFYING = "verifying"
STATE_CLOSED = "closed"


# Per-kind allowed resolutions for a CLOSE transition. Mirrors v1's
# ALLOWED_RESOLUTIONS_BY_KIND (conversation_schemas.py) so v2's state
# machine doesn't disagree with the authoritative v1 path during the
# dual-write era. ``abandoned`` is added on top of the v1 vocab for
# every closeable kind because the system-bypass auto-abandon path
# (idle sweeper) closes with that resolution regardless of kind.
ALLOWED_RESOLUTIONS: dict[str, frozenset[str]] = {
    KIND_INQUIRY: frozenset({"answered", "dropped", "escalated", "abandoned"}),
    KIND_PROPOSAL: frozenset({"accepted", "rejected", "withdrawn", "superseded", "abandoned"}),
    KIND_TASK: frozenset({"completed", "failed", "cancelled", "abandoned"}),
    KIND_GENERAL: frozenset(),  # general doesn't close
}


# Per-kind allowed (from_state, to_state) transitions, NOT including the
# "close" leg which is governed separately by ALLOWED_RESOLUTIONS.
ALLOWED_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    KIND_GENERAL: {STATE_OPEN: frozenset()},
    KIND_INQUIRY: {STATE_OPEN: frozenset()},
    KIND_PROPOSAL: {STATE_OPEN: frozenset()},
    KIND_TASK: {
        STATE_OPEN: frozenset({STATE_CLAIMED}),
        STATE_CLAIMED: frozenset({STATE_EXECUTING, STATE_OPEN}),  # release re-opens
        STATE_EXECUTING: frozenset({
            STATE_BLOCKED_APPROVAL, STATE_VERIFYING, STATE_CLAIMED,
        }),
        STATE_BLOCKED_APPROVAL: frozenset({STATE_EXECUTING, STATE_CLAIMED}),
        STATE_VERIFYING: frozenset({STATE_EXECUTING}),
    },
}


class StateMachineError(ValueError):
    pass


@dataclass(frozen=True)
class TransitionDecision:
    allowed: bool
    reason: str = ""
    forced_close: bool = False


class OperationStateMachine:
    """Pure validator -- no DB access, no side effects. Callers ask
    'can I move op(kind=K, state=S) to T (resolution=R)?' and act on
    the decision.

    Two entry points:
      - ``can_transition(kind, from_state, to_state)`` for non-close
        moves (claim, executing, etc.)
      - ``can_close(kind, from_state, resolution, *, system=False)`` for
        the close leg. ``system=True`` lets auto-abandon by the idle
        sweeper close from any non-terminal state with
        resolution='abandoned' regardless of kind vocab.
    """

    def can_transition(
        self,
        *,
        kind: str,
        from_state: str,
        to_state: str,
    ) -> TransitionDecision:
        if kind not in ALLOWED_TRANSITIONS:
            return TransitionDecision(False, f"unknown kind {kind!r}")
        per_state = ALLOWED_TRANSITIONS[kind]
        if from_state not in per_state:
            return TransitionDecision(False, f"unknown from_state {from_state!r} for kind={kind}")
        targets = per_state[from_state]
        if to_state not in targets:
            allowed = sorted(targets) or ["<none>"]
            return TransitionDecision(
                False,
                f"{kind}: {from_state} -> {to_state} not allowed; "
                f"valid next states: {allowed}",
            )
        return TransitionDecision(True)

    def can_close(
        self,
        *,
        kind: str,
        from_state: str,
        resolution: str,
        system: bool = False,
    ) -> TransitionDecision:
        if from_state == STATE_CLOSED:
            return TransitionDecision(False, "operation already closed")
        if kind == KIND_GENERAL:
            return TransitionDecision(False, "general operations cannot be closed")
        # System bypass: idle sweeper / system-level close.
        if system and resolution == "abandoned":
            return TransitionDecision(True, forced_close=True)
        allowed = ALLOWED_RESOLUTIONS.get(kind, frozenset())
        if resolution not in allowed:
            return TransitionDecision(
                False,
                f"resolution {resolution!r} not allowed for kind={kind}; "
                f"valid: {sorted(allowed) or '<none>'}",
            )
        return TransitionDecision(True)

    def assert_transition(self, **kwargs) -> None:
        d = self.can_transition(**kwargs)
        if not d.allowed:
            raise StateMachineError(d.reason)

    def assert_close(self, **kwargs) -> None:
        d = self.can_close(**kwargs)
        if not d.allowed:
            raise StateMachineError(d.reason)
