"""Chat behavior schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ChatThreadSummary(BaseModel):
    id: str
    discord_thread_id: str
    title: str
    topic: str | None = None
    status: str
    turn_count: int
    last_actor_name: str | None = None
    last_message_preview: str | None = None
    last_message_at: datetime | None = None
    created_at: datetime


class ChatThreadCreateResponse(BaseModel):
    id: str
    discord_thread_id: str
    title: str


class ChatParticipantSummary(BaseModel):
    actor_name: str
    actor_kind: str
    turn_count: int
    last_message_preview: str | None = None
    last_message_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_read_message_id: str | None = None
    last_read_message_at: datetime | None = None


class ChatMessageSummary(BaseModel):
    id: str
    actor_name: str
    event_kind: str
    content: str
    created_at: datetime


class ChatParticipantRegisterRequest(BaseModel):
    actor_name: str
    actor_kind: str = "ai"


class ChatParticipantHeartbeatRequest(BaseModel):
    actor_name: str


class ChatMessageSubmitRequest(BaseModel):
    actor_name: str
    actor_kind: str = "ai"
    content: str


class ChatMessageSubmitResponse(BaseModel):
    thread: ChatThreadSummary
    participant: ChatParticipantSummary
    message: ChatMessageSummary


class ChatThreadDeltaResponse(BaseModel):
    thread: ChatThreadSummary
    participant: ChatParticipantSummary
    participants: list[ChatParticipantSummary]
    messages: list[ChatMessageSummary]
