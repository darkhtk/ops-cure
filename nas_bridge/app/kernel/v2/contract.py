"""Protocol v2 + v3-additive Contract — single source of truth for vocab,
state, and rules.

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

from typing import Any


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
    # v3-additive governance acts. ``move_close`` is "I move we close
    # this op with resolution X"; ``ratify`` is "I approve closing"
    # (typically a reply to a prior move_close). The bridge's policy
    # engine consults these to enforce non-unilateral close policies.
    "move_close",
    "ratify",
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


# ----- protocol v3 primitives (additive in this phase) -------------------
# Two new constructs were missing in the original speech-act schema:
#   1. expected_response on each event (who is expected to reply, with
#      what speech kind, by when), so cascade prevention + reply-target
#      resolution become mechanical instead of heuristic.
#   2. operation policy (close rule, max rounds, member admission,
#      context compaction) so an op is a *governed process*, not just
#      an event log.
#
# These are accepted optionally everywhere and stored inside existing
# JSON columns (event payload._meta.expected_response, op metadata.policy)
# so no schema migration is required for phase-1 introduction.

# expected_response.kinds: subset of SPEECH_KINDS the responder may
# choose from. Empty/missing => any speech kind. The literal "*" sentinel
# is also accepted to mean "any kind".
EXPECTED_RESPONSE_KIND_WILDCARD = "*"


def validate_expected_response(value: dict | None) -> dict | None:
    """Normalize and validate an expected_response payload. Returns the
    normalized dict (or None if the input is empty/None). Raises
    ValueError on shape violations.

    Shape:
      {
        "from_actor_handles": [str, ...]  # who is expected to reply
        "kinds": [speech_kind, ...]?      # restricted reply kinds, or "*"
        "by_round_seq": int?              # if responder hasn't replied
                                          # by this op-event seq, the op
                                          # is considered to have a
                                          # pending defer (caller policy)
      }
    """
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise ValueError("expected_response must be a dict")
    out: dict[str, Any] = {}
    handles = value.get("from_actor_handles") or value.get("from") or []
    if isinstance(handles, str):
        handles = [handles]
    if not isinstance(handles, list):
        raise ValueError("expected_response.from_actor_handles must be a list")
    norm_handles: list[str] = []
    for h in handles:
        if not isinstance(h, str) or not h:
            raise ValueError("expected_response.from_actor_handles entries must be non-empty strings")
        norm_handles.append(h if h.startswith("@") else f"@{h}")
    out["from_actor_handles"] = norm_handles
    kinds = value.get("kinds")
    if kinds is not None:
        if isinstance(kinds, str):
            kinds = [kinds]
        if not isinstance(kinds, list):
            raise ValueError("expected_response.kinds must be a list")
        for k in kinds:
            if k != EXPECTED_RESPONSE_KIND_WILDCARD and k not in SPEECH_KINDS:
                raise ValueError(f"expected_response.kinds: unknown speech kind {k!r}")
        out["kinds"] = list(kinds)
    by = value.get("by_round_seq")
    if by is not None:
        if not isinstance(by, int) or by < 0:
            raise ValueError("expected_response.by_round_seq must be a non-negative int")
        out["by_round_seq"] = by
    return out


# Operation policy controls op-level governance. None of the fields are
# enforced in phase 1 (we just persist), but the shape is fixed so phase 2
# can switch on it without schema churn.
CLOSE_POLICY_OPENER_UNILATERAL = "opener_unilateral"  # current behavior
CLOSE_POLICY_ANY_PARTICIPANT = "any_participant"
CLOSE_POLICY_QUORUM = "quorum"  # parameterized by min_ratifiers
CLOSE_POLICY_OPERATOR_RATIFIES = "operator_ratifies"  # @operator role required

ALL_CLOSE_POLICIES: frozenset[str] = frozenset({
    CLOSE_POLICY_OPENER_UNILATERAL,
    CLOSE_POLICY_ANY_PARTICIPANT,
    CLOSE_POLICY_QUORUM,
    CLOSE_POLICY_OPERATOR_RATIFIES,
})

JOIN_POLICY_INVITE_ONLY = "invite_only"
JOIN_POLICY_SELF_OR_INVITE = "self_or_invite"
JOIN_POLICY_OPEN = "open"

ALL_JOIN_POLICIES: frozenset[str] = frozenset({
    JOIN_POLICY_INVITE_ONLY,
    JOIN_POLICY_SELF_OR_INVITE,
    JOIN_POLICY_OPEN,
})

CONTEXT_COMPACTION_NONE = "none"
CONTEXT_COMPACTION_ROLLING_SUMMARY = "rolling_summary"

ALL_CONTEXT_COMPACTIONS: frozenset[str] = frozenset({
    CONTEXT_COMPACTION_NONE,
    CONTEXT_COMPACTION_ROLLING_SUMMARY,
})


DEFAULT_OPERATION_POLICY: dict = {
    "close_policy": CLOSE_POLICY_OPENER_UNILATERAL,
    "join_policy": JOIN_POLICY_SELF_OR_INVITE,
    "context_compaction": CONTEXT_COMPACTION_NONE,
    "max_rounds": None,            # None = unbounded
    "min_ratifiers": None,         # only used when close_policy=quorum
    "bot_open": True,              # bots are first-class openers by default
}


def validate_operation_policy(value: dict | None) -> dict:
    """Normalize an operation policy. Falls back to DEFAULT_OPERATION_POLICY
    for missing keys. Raises ValueError on unknown enum values."""
    base = dict(DEFAULT_OPERATION_POLICY)
    if value:
        if not isinstance(value, dict):
            raise ValueError("policy must be a dict")
        cp = value.get("close_policy")
        if cp is not None:
            if cp not in ALL_CLOSE_POLICIES:
                raise ValueError(f"policy.close_policy: unknown {cp!r}")
            base["close_policy"] = cp
        jp = value.get("join_policy")
        if jp is not None:
            if jp not in ALL_JOIN_POLICIES:
                raise ValueError(f"policy.join_policy: unknown {jp!r}")
            base["join_policy"] = jp
        cc = value.get("context_compaction")
        if cc is not None:
            if cc not in ALL_CONTEXT_COMPACTIONS:
                raise ValueError(f"policy.context_compaction: unknown {cc!r}")
            base["context_compaction"] = cc
        mr = value.get("max_rounds")
        if mr is not None:
            if not isinstance(mr, int) or mr <= 0:
                raise ValueError("policy.max_rounds must be a positive int")
            base["max_rounds"] = mr
        mq = value.get("min_ratifiers")
        if mq is not None:
            if not isinstance(mq, int) or mq <= 0:
                raise ValueError("policy.min_ratifiers must be a positive int")
            base["min_ratifiers"] = mq
        bo = value.get("bot_open")
        if bo is not None:
            if not isinstance(bo, bool):
                raise ValueError("policy.bot_open must be a bool")
            base["bot_open"] = bo
    if base["close_policy"] == CLOSE_POLICY_QUORUM and not base.get("min_ratifiers"):
        raise ValueError("policy.close_policy=quorum requires min_ratifiers")
    return base


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
