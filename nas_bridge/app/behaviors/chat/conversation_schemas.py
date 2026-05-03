"""Schemas for the chat conversation protocol layer.

The protocol distinguishes:

- ``Conversation`` — a unit of collaboration with explicit open/close,
  a kind, and a resolution. Three product kinds plus the implicit
  ``general`` always-open kind:

    * ``inquiry``  — information request
    * ``proposal`` — proposed action or decision (accepted/rejected closes)
    * ``task``     — execution unit (binds to ``RemoteTaskService`` in PR2)
    * ``general``  — the always-open casual stream per room

- ``SpeechAct`` — a single utterance inside a conversation. Replaces the
  old free-form ``ChatMessageModel.event_kind="message"`` shape with a
  typed kind set so a reader can tell a question from a claim from
  evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


SpeechKind = Literal[
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
    # PR20: a low-cost acknowledgement (👍, "ack", "noted") that
    # doesn't deserve a full agree/object speech act but still
    # registers presence + intent. Useful for reducing noise on
    # "I see you" turns.
    "react",
    # v3 governance acts. The bridge's policy engine treats these
    # specially when computing close admissibility.
    "move_close",
    "ratify",
    # v3 phase 2.5 membership acts. ``invite`` is opener/participant
    # admitting another handle; ``join`` is an actor self-declaring
    # participation (gated by policy.join_policy).
    "invite",
    "join",
]


# Evidence kind allow-list. Closes GAP #09 from
# scripts/failure_mode_scenarios.py: previously kind was a free-form
# string and an AI could post evidence with kind="trust_me_bro".
# These are the kinds the agent contract documents + the WORK kinds
# from RemoteTaskService that auto-promote task status.
EvidenceKind = Literal[
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
]

ConversationKind = Literal["inquiry", "proposal", "task", "general"]
ConversationState = Literal["open", "resolving", "closed"]


# γ migration: vocab is now sourced from kernel.v2.contract. The
# v1 chat layer and the v2 state machine MUST agree on these sets;
# importing from the contract is the single mechanism that prevents
# drift.
from ...kernel.v2 import contract as _v2_contract

ALLOWED_RESOLUTIONS_BY_KIND: dict[str, frozenset[str]] = _v2_contract.ALLOWED_RESOLUTIONS


def is_resolution_allowed(*, kind: str, resolution: str) -> bool:
    allowed = ALLOWED_RESOLUTIONS_BY_KIND.get(kind)
    if not allowed:
        # general (empty set) or unknown kind: skip enforcement
        return True
    return resolution in allowed


# Drift detector: pydantic Literal types can't be built from runtime
# data, but we can assert the value list matches contract on module
# load. Adding a new SpeechKind / EvidenceKind requires updating BOTH
# the Literal here AND the contract; this guard makes the failure
# loud and immediate.
def _assert_literal_matches_contract(literal_alias, contract_set, label: str) -> None:
    from typing import get_args
    schema_args = set(get_args(literal_alias))
    if schema_args != set(contract_set):
        only_schema = schema_args - set(contract_set)
        only_contract = set(contract_set) - schema_args
        raise AssertionError(
            f"{label} drift: only-in-schema={sorted(only_schema)}, "
            f"only-in-contract={sorted(only_contract)}"
        )


# ----- summaries / responses --------------------------------------------------


class SpeechActSummary(BaseModel):
    id: str
    conversation_id: str
    actor_name: str
    kind: str
    content: str
    addressed_to: str | None = None
    addressed_to_many: list[str] = Field(default_factory=list)
    replies_to_speech_id: str | None = None
    created_at: datetime


class ConversationSummary(BaseModel):
    id: str
    thread_id: str
    kind: str
    title: str
    intent: str | None = None
    state: str
    opener_actor: str
    owner_actor: str | None = None
    expected_speaker: str | None = None
    parent_conversation_id: str | None = None
    bound_task_id: str | None = None
    resolution: str | None = None
    resolution_summary: str | None = None
    closed_by: str | None = None
    is_general: bool
    last_speech_at: datetime | None = None
    speech_count: int
    idle_warning_emitted_at: datetime | None = None
    idle_warning_count: int = 0
    unaddressed_speech_count: int = 0
    created_at: datetime
    closed_at: datetime | None = None


class ConversationDetailResponse(BaseModel):
    conversation: ConversationSummary
    recent_speech: list[SpeechActSummary] = Field(default_factory=list)


class ConversationListResponse(BaseModel):
    thread_id: str
    conversations: list[ConversationSummary] = Field(default_factory=list)


# ----- requests ---------------------------------------------------------------


class ConversationOpenRequest(BaseModel):
    kind: Literal["inquiry", "proposal", "task"]
    title: str = Field(min_length=1, max_length=200)
    opener_actor: str = Field(min_length=1)
    intent: str | None = None
    owner_actor: str | None = None
    addressed_to: str | None = None
    parent_conversation_id: str | None = None
    # Only meaningful when kind == "task". A bound RemoteTaskModel row is
    # created through RemoteTaskService and its id stored on
    # ``bound_task_id``. Ignored for inquiry / proposal.
    objective: str | None = None
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    priority: str = "normal"
    # v3-additive: optional governance policy for this op.
    # See kernel.v2.contract.DEFAULT_OPERATION_POLICY for shape +
    # defaults. Stored under op metadata.policy on dual-write so the
    # bridge can later enforce close/quorum/compaction rules without
    # a v3 schema migration.
    policy: dict[str, Any] | None = None

    @field_validator("title", "intent", "opener_actor", "owner_actor", "addressed_to", "objective")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class SpeechActSubmitRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    actor_kind: str = "ai"
    kind: SpeechKind = "claim"
    content: str = Field(min_length=1)
    addressed_to: str | None = None
    # PR20: additional addressees beyond the primary ``addressed_to``.
    # The canonical single slot (``addressed_to``) drives
    # expected_speaker; the full list is stored alongside for
    # renderers + callers that want to show "@a @b @c" without losing
    # who's the round's primary expected respondent.
    addressed_to_many: list[str] = Field(default_factory=list)
    # PR15: optional pointer to a prior speech act this one replies
    # to. The receiving service does not validate that the parent
    # speech belongs to the same conversation -- callers are expected
    # to keep that consistent (cross-conversation replies could arise
    # from cross-references like "see #other-conv message-X").
    replies_to_speech_id: str | None = None
    # v3-additive: same pointer but expressed as the v2 event id.
    # External /v2 callers don't have v1 message ids; the bridge
    # resolves this at submit time so the reply chain lands BEFORE
    # the broker fan-out (so SSE subscribers see it in real time, not
    # after a re-fetch).
    replies_to_v2_event_id: str | None = None
    # F6 (v2-only): when set, the speech is whispered to these actors
    # only. The v1 ChatMessageModel stores no privacy bit -- it always
    # records the message. The v2 OperationEvent.private_to_actor_ids
    # is what enforces redaction. v2 readers MUST honor this list;
    # v1 readers (chat API GET /messages) currently do not (will be
    # closed in F7 reader transition).
    private_to_actors: list[str] = Field(default_factory=list)
    # v3-additive: declare who is expected to respond, with what speech
    # kinds, and by which round (op event seq).
    # See kernel.v2.contract.validate_expected_response. Stored under
    # event payload._meta.expected_response so external SSE consumers
    # can drive cascade-prevention without inspecting any extra
    # metadata column.
    expected_response: dict[str, Any] | None = None

    @field_validator("addressed_to")
    @classmethod
    def _strip_addressed_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("addressed_to_many")
    @classmethod
    def _normalize_addressed_to_many(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = (item or "").strip()
            if not text or text in seen:
                continue
            cleaned.append(text)
            seen.add(text)
        return cleaned


class ConversationCloseRequest(BaseModel):
    closed_by: str = Field(min_length=1)
    resolution: str = Field(min_length=1, max_length=80)
    summary: str | None = None

    @field_validator("resolution")
    @classmethod
    def _normalize_resolution(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "_")


# ----- task lifecycle (kind=task only) ---------------------------------------


class ChatTaskClaimRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    lease_seconds: int = 120

    @field_validator("lease_seconds")
    @classmethod
    def _bound_lease(cls, value: int) -> int:
        return max(10, min(value, 3600))


class ChatTaskHeartbeatRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    summary: str | None = None
    commands_run_count: int = 0
    files_read_count: int = 0
    files_modified_count: int = 0
    tests_run_count: int = 0
    lease_seconds: int = 120

    @field_validator(
        "commands_run_count",
        "files_read_count",
        "files_modified_count",
        "tests_run_count",
        "lease_seconds",
    )
    @classmethod
    def _non_negative(cls, value: int) -> int:
        return max(0, value)


class ChatTaskEvidenceRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    # Closes GAP #07: evidence injection without lease check. The
    # coordinator validates lease_token + actor against the current
    # task assignment before persisting the row.
    lease_token: str = Field(min_length=1)
    # Closes GAP #09: kind is now enum-validated.
    kind: EvidenceKind
    summary: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatTaskCompleteRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    summary: str | None = None


class ChatTaskFailRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    error_text: str = Field(min_length=1)


class ChatTaskStateResponse(BaseModel):
    """Combined response shape for task lifecycle endpoints. Returns the
    underlying RemoteTask payload (already in Opscure shape) plus a
    snapshot of the bound conversation so callers can verify
    auto-close happened on terminal transitions."""

    conversation: ConversationSummary
    task: dict[str, Any]


# ---- approval / interrupt / note (PR14) -----------------------------------


class ChatTaskInterruptRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    note: str | None = None


class ChatTaskApprovalRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    note: str | None = None


class ChatTaskApprovalResolveRequest(BaseModel):
    resolved_by: str = Field(min_length=1)
    resolution: str = Field(min_length=1)
    note: str | None = None

    @field_validator("resolution")
    @classmethod
    def _normalize_resolution(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in {"approved", "denied"}:
            raise ValueError("resolution must be 'approved' or 'denied'")
        return text


class ChatTaskNoteRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    kind: str = "note"
    content: str = Field(min_length=1)


class ChatTaskNoteResponse(BaseModel):
    """Notes are coordination-only; they do not change task state."""
    conversation: ConversationSummary
    note: dict[str, Any]


# γ migration -- run at module import. If a future PR adds a value to
# either the Literal or the contract without updating the other, the
# bridge fails to start with a clear error pointing at the offender.
_assert_literal_matches_contract(SpeechKind, _v2_contract.SPEECH_KINDS, "SpeechKind")
_assert_literal_matches_contract(EvidenceKind, _v2_contract.EVIDENCE_KINDS, "EvidenceKind")


# ----- handoff & idle-sweep --------------------------------------------------


class ConversationHandoffRequest(BaseModel):
    by_actor: str = Field(min_length=1)
    new_owner: str = Field(min_length=1)
    reason: str | None = None

    @field_validator("by_actor", "new_owner", "reason")
    @classmethod
    def _strip_handoff(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class MetricSnapshotEntry(BaseModel):
    id: str
    thread_id: str | None = None
    captured_at: datetime
    snapshot: dict[str, Any] = Field(default_factory=dict)


class MetricHistoryResponse(BaseModel):
    items: list[MetricSnapshotEntry] = Field(default_factory=list)


class LatencyStatsResponse(BaseModel):
    """Aggregate latency over the last N closed conversations. All
    times in seconds. Counts per kind so you can see which kinds
    close fastest."""
    thread_id: str | None = None
    sample_size: int
    by_kind: dict[str, dict[str, float]] = Field(default_factory=dict)
    overall: dict[str, float] = Field(default_factory=dict)


class ConversationMarkReadRequest(BaseModel):
    actor_name: str = Field(min_length=1)
    speech_id: str | None = None  # if None, mark to latest


class ConversationReadStatusResponse(BaseModel):
    conversation_id: str
    actor_name: str
    last_read_speech_id: str | None = None
    last_read_at: datetime | None = None
    unread_count: int = 0


class IdleSweepResponse(BaseModel):
    thread_id: str
    idle_threshold_seconds: int
    flagged: list[ConversationSummary] = Field(default_factory=list)


class BulkCloseRequest(BaseModel):
    """Operator bulk close of N conversations with the same
    resolution. Each conversation is closed independently; failures
    on individual rows are reported per-id rather than aborting the
    whole call."""
    conversation_ids: list[str] = Field(min_length=1)
    closed_by: str = Field(min_length=1)
    resolution: str = Field(min_length=1, max_length=80)
    summary: str | None = None
    bypass_task_guard: bool = False


class BulkCloseResultItem(BaseModel):
    conversation_id: str
    ok: bool
    resolution: str | None = None
    error: str | None = None


class BulkCloseResponse(BaseModel):
    requested: int
    succeeded: int
    failed: int
    results: list[BulkCloseResultItem] = Field(default_factory=list)


class AuditLogEntry(BaseModel):
    id: str
    thread_id: str
    conversation_id: str | None = None
    actor_name: str
    event_kind: str
    addressed_to: str | None = None
    content: str
    created_at: datetime


class AuditLogResponse(BaseModel):
    items: list[AuditLogEntry] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


class ChatRoomHealthResponse(BaseModel):
    """Per-thread health snapshot. ``open_conversations`` and
    ``idle_candidates`` are derived live from the DB; ``metrics`` is
    the global in-memory counter snapshot (covers all threads on
    this bridge instance, not just the requested one)."""

    thread_id: str
    open_conversations: int
    idle_candidates: int
    expected_speakers: list[str] = Field(default_factory=list)
    bound_active_tasks: int
    metrics: dict[str, Any] = Field(default_factory=dict)
