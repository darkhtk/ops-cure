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
from typing import Literal

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
    "address",
]

ConversationKind = Literal["inquiry", "proposal", "task", "general"]
ConversationState = Literal["open", "resolving", "closed"]


# ----- summaries / responses --------------------------------------------------


class SpeechActSummary(BaseModel):
    id: str
    conversation_id: str
    actor_name: str
    kind: str
    content: str
    addressed_to: str | None = None
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

    @field_validator("title", "intent", "opener_actor", "owner_actor", "addressed_to")
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

    @field_validator("addressed_to")
    @classmethod
    def _strip_addressed_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class ConversationCloseRequest(BaseModel):
    closed_by: str = Field(min_length=1)
    resolution: str = Field(min_length=1, max_length=80)
    summary: str | None = None

    @field_validator("resolution")
    @classmethod
    def _normalize_resolution(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "_")
