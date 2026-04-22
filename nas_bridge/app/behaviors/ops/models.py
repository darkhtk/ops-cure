"""Ops behavior persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OpsThreadModel(Base):
    __tablename__ = "ops_threads"
    __table_args__ = (
        Index("ix_ops_threads_parent_status", "parent_channel_id", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    guild_id: Mapped[str] = mapped_column(index=True)
    parent_channel_id: Mapped[str] = mapped_column(index=True)
    discord_thread_id: Mapped[str] = mapped_column(unique=True, index=True)
    title: Mapped[str] = mapped_column(Text())
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(index=True, default="monitoring")
    issue_count: Mapped[int] = mapped_column(Integer(), default=0)
    note_count: Mapped[int] = mapped_column(Integer(), default=0)
    last_actor_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_event_kind: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_event_preview: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OpsParticipantModel(Base):
    __tablename__ = "ops_participants"
    __table_args__ = (
        UniqueConstraint("thread_id", "actor_name", name="uq_ops_participant_per_thread"),
        Index("ix_ops_participants_thread_last_event", "thread_id", "last_event_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("ops_threads.id", ondelete="CASCADE"), index=True)
    actor_name: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(Text(), default="operator")
    event_count: Mapped[int] = mapped_column(Integer(), default=0)
    last_event_preview: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OpsEventModel(Base):
    __tablename__ = "ops_events"
    __table_args__ = (
        Index("ix_ops_events_thread_created", "thread_id", "created_at"),
        Index("ix_ops_events_thread_kind_created", "thread_id", "event_kind", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("ops_threads.id", ondelete="CASCADE"), index=True)
    actor_name: Mapped[str] = mapped_column(index=True)
    event_kind: Mapped[str] = mapped_column(Text(), default="note")
    content: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
