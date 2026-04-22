"""Ops behavior schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OpsThreadSummary(BaseModel):
    id: str
    discord_thread_id: str
    title: str
    summary: str | None = None
    status: str
    issue_count: int
    note_count: int
    last_actor_name: str | None = None
    last_event_kind: str | None = None
    last_event_preview: str | None = None
    last_event_at: datetime | None = None
    created_at: datetime


class OpsThreadCreateResponse(BaseModel):
    id: str
    discord_thread_id: str
    title: str
