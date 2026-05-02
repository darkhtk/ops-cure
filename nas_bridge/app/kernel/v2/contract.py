"""Protocol v2 Contract — single source of truth for vocab, state, and rules.

Every speech kind, evidence kind, resolution vocabulary, state transition,
default capability, and event-to-state mapping that the v2 protocol
recognizes lives in this module. All other modules (v1 schemas, v2
schemas, state machine, capability service, mirror, chat behaviors)
import from here -- they MUST NOT define their own copy.

The drift problem this solves:

  Before this module, the same vocabulary was defined in 2-3 places
  (e.g. ``ALLOWED_RESOLUTIONS_BY_KIND`` in conversation_schemas + a
  parallel set in state_machine), and they had already drifted twice
  during F1->F11. With this module, drift is impossible at the data
  layer; pydantic ``Literal`` typed fields keep their hardcoded
  enums for static checking, but assert against contract at module
  load so any future addition shows up loudly.

Self-consistency assertions run on first import (validate_contract()):

  - every operation kind in ALL_KINDS has both a resolution set and
    a transition graph entry
  - every state mentioned by EVENT_KIND_TO_TARGET_STATE actually
    appears as a transition target somewhere
  - every non-OPEN from_state in ALLOWED_TRANSITIONS is reachable
    via some other transition (no orphan states)

Future additions to v2 (G5 hierarchy, F11 task lifecycle on native
v2, capability inheritance) all start by editing THIS module.
"""
from __future__ import annotations


# ----- operation kinds ----------------------------------------------------
KIND_GENERAL = "general"
KIND_INQUIRY = "inquiry"
KIND_PROPOSAL = "proposal"
KIND_TASK = "task"

ALL_KINDS: tuple[str, ...] = (KIND_GENERAL, KIND_INQUIRY, KIND_PROPOSAL, KIND_TASK)

CLOSEABLE_KINDS: frozenset[str] = frozenset({KIND_INQUIRY, KIND_PROPOSAL, KIND_TASK})


# ----- operation states ---------------------------------------------------
STATE_OPEN = "open"
STATE_CLAIMED = "claimed"
STATE_EXECUTING = "executing"
STATE_BLOCKED_APPROVAL = "blocked_approval"
STATE_VERIFYING = "verifying"
STATE_CLOSED = "closed"


# ----- speech kinds -------------------------------------------------------
# Static set. ``conversation_schemas.SpeechKind`` is a pydantic Literal
# whose value list MUST match this set; an assert at module load detects
# drift the moment either side adds a value without updating the other.
SPEECH_KINDS: frozenset[str] = frozenset({
    "claim",
    "question",
    "answer",
    "propose",
    "agree",
    "object",
    "evidence",
    "block",
    "defer",
    "summarize",
    # PR20: low-cost ack ("noted", thumbs-up). Reduces noise on
    # "I see you" turns without forcing a full agree/object.
    "react",
})


# ----- evidence kinds -----------------------------------------------------
# Same drift-detection pattern as SPEECH_KINDS.
EVIDENCE_KINDS: frozenset[str] = frozenset({
    "command_execution",
    "file_read",
    "file_write",
    "test_result",
    "screenshot",
    "approval_request",
    "error",
    "result",
    "runtime_turn_started",
    "runtime_turn_completed",
})


# ----- resolution vocab per kind -----------------------------------------
# 'abandoned' is universally allowed as the system-bypass closure
# (idle sweeper auto-abandon, task coordinator denial cascade, etc.)
# so it appears in every closeable kind's set. State machine's
# ``can_close(system=True, resolution='abandoned')`` honors that
# regardless of kind for back-compat with PR-era idle escalation.
ALLOWED_RESOLUTIONS: dict[str, frozenset[str]] = {
    KIND_INQUIRY:  frozenset({"answered", "dropped", "escalated", "abandoned"}),
    KIND_PROPOSAL: frozenset({"accepted", "rejected", "withdrawn", "superseded", "abandoned"}),
    KIND_TASK:     frozenset({"completed", "failed", "cancelled", "abandoned"}),
    KIND_GENERAL:  frozenset(),  # general doesn't close
}


# ----- state graph per kind ----------------------------------------------
# (kind -> {from_state: frozenset(allowed to_states)}). The CLOSE leg
# is governed by ALLOWED_RESOLUTIONS, not this graph -- callers go
# through OperationStateMachine.can_close() for that.
ALLOWED_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    KIND_GENERAL:  {STATE_OPEN: frozenset()},
    KIND_INQUIRY:  {STATE_OPEN: frozenset()},
    KIND_PROPOSAL: {STATE_OPEN: frozenset()},
    KIND_TASK: {
        STATE_OPEN:             frozenset({STATE_CLAIMED}),
        STATE_CLAIMED:          frozenset({STATE_EXECUTING, STATE_OPEN}),
        STATE_EXECUTING:        frozenset({STATE_BLOCKED_APPROVAL, STATE_VERIFYING, STATE_CLAIMED}),
        STATE_BLOCKED_APPROVAL: frozenset({STATE_EXECUTING, STATE_CLAIMED}),
        STATE_VERIFYING:        frozenset({STATE_EXECUTING}),
    },
}


# ----- event_kind -> target state ----------------------------------------
# When a chat lifecycle event is mirrored to v2, this table tells
# the mirror which target state the operation should transition to
# (None = no auto-transition; close is handled separately). Today
# ``ChatTaskCoordinator._update_owner_and_emit`` passes new_v2_state
# explicitly per call site; the table is the seed for replacing those
# 4 hardcoded sites with one lookup. Approval-resolved depends on the
# resolution payload (approved -> executing, denied -> auto-close).
EVENT_KIND_TO_TARGET_STATE: dict[str, str] = {
    "chat.task.claimed":             STATE_CLAIMED,
    "chat.task.evidence":            STATE_EXECUTING,
    "chat.task.approval_requested":  STATE_BLOCKED_APPROVAL,
    # approval_resolved is conditional on payload.resolution; mirror
    # passes new_v2_state explicitly there. Keeping it absent here so
    # automation never silently misroutes a denied approval.
}


# ----- capabilities -------------------------------------------------------
CAP_CONVERSATION_OPEN = "conversation.open"
CAP_CONVERSATION_CLOSE = "conversation.close"
CAP_CONVERSATION_CLOSE_OPENER = "conversation.close.opener"
CAP_CONVERSATION_HANDOFF = "conversation.handoff"
CAP_SPEECH_SUBMIT = "speech.submit"
CAP_TASK_CLAIM = "task.claim"
CAP_TASK_COMPLETE = "task.complete"
CAP_TASK_FAIL = "task.fail"
CAP_TASK_APPROVE_DESTRUCTIVE = "task.approve.destructive"

ALL_CAPABILITIES: frozenset[str] = frozenset({
    CAP_CONVERSATION_OPEN,
    CAP_CONVERSATION_CLOSE,
    CAP_CONVERSATION_CLOSE_OPENER,
    CAP_CONVERSATION_HANDOFF,
    CAP_SPEECH_SUBMIT,
    CAP_TASK_CLAIM,
    CAP_TASK_COMPLETE,
    CAP_TASK_FAIL,
    CAP_TASK_APPROVE_DESTRUCTIVE,
})


# Default capability sets per actor kind. Used by ActorService when
# auto-provisioning a fresh actor row, before any explicit grants.
DEFAULT_CAPABILITIES_HUMAN: tuple[str, ...] = (
    CAP_CONVERSATION_OPEN,
    CAP_CONVERSATION_CLOSE,
    CAP_CONVERSATION_CLOSE_OPENER,
    CAP_CONVERSATION_HANDOFF,
    CAP_SPEECH_SUBMIT,
    CAP_TASK_CLAIM,
    CAP_TASK_COMPLETE,
    CAP_TASK_FAIL,
)

DEFAULT_CAPABILITIES_AI: tuple[str, ...] = (
    CAP_CONVERSATION_OPEN,
    CAP_CONVERSATION_CLOSE_OPENER,
    CAP_CONVERSATION_HANDOFF,
    CAP_SPEECH_SUBMIT,
    CAP_TASK_CLAIM,
    CAP_TASK_COMPLETE,
    CAP_TASK_FAIL,
)


def validate_contract() -> None:
    """Self-consistency check. Fails fast at import if the contract
    is internally broken (missing kind, orphan state, target state
    that no transition reaches)."""

    # Every kind has resolution set + transition graph.
    for k in ALL_KINDS:
        if k not in ALLOWED_RESOLUTIONS:
            raise AssertionError(f"contract: kind {k!r} missing from ALLOWED_RESOLUTIONS")
        if k not in ALLOWED_TRANSITIONS:
            raise AssertionError(f"contract: kind {k!r} missing from ALLOWED_TRANSITIONS")

    # Every event-driven target state appears as some kind's transition target.
    all_targets: set[str] = set()
    for kind_graph in ALLOWED_TRANSITIONS.values():
        for targets in kind_graph.values():
            all_targets.update(targets)
    for ev_kind, target in EVENT_KIND_TO_TARGET_STATE.items():
        if target not in all_targets:
            raise AssertionError(
                f"contract: event {ev_kind!r} -> state {target!r} but state "
                f"never appears as a transition target in any kind"
            )

    # No orphan from_state (except OPEN, the entry point).
    for kind, graph in ALLOWED_TRANSITIONS.items():
        for from_state in graph:
            if from_state == STATE_OPEN:
                continue
            reachable = any(
                from_state in tgts for tgts in graph.values()
            )
            if not reachable:
                raise AssertionError(
                    f"contract: orphan state in {kind!r}: {from_state!r} "
                    f"is a from_state but never appears as any target"
                )

    # Default capability sets are subsets of ALL_CAPABILITIES.
    for label, caps in (
        ("DEFAULT_CAPABILITIES_HUMAN", DEFAULT_CAPABILITIES_HUMAN),
        ("DEFAULT_CAPABILITIES_AI", DEFAULT_CAPABILITIES_AI),
    ):
        for c in caps:
            if c not in ALL_CAPABILITIES:
                raise AssertionError(
                    f"contract: {label} contains unknown capability {c!r}"
                )


validate_contract()
