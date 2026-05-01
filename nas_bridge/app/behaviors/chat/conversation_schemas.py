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


# Per-kind resolution vocabulary. Closing a conversation with a
# resolution outside the allowed set for its kind is rejected so the
# semantic layer cannot drift via free-form strings (the previous v0.1
# accepted any string).
ALLOWED_RESOLUTIONS_BY_KIND: dict[str, frozenset[str]] = {
    "inquiry": frozenset({"answered", "dropped", "escalated"}),
    "proposal": frozenset({"accepted", "rejected", "withdrawn", "superseded"}),
    "task": frozenset({"completed", "failed", "cancelled", "abandoned"}),
}


def is_resolution_allowed(*, kind: str, resolution: str) -> bool:
    allowed = ALLOWED_RESOLUTIONS_BY_KIND.get(kind)
    if allowed is None:
        return True  # general or unknown kind: skip enforcement
    return resolution in allowed


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
