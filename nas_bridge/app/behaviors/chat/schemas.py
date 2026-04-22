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
