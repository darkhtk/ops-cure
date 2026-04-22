"""Chat behavior persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatThreadModel(Base):
    __tablename__ = "chat_threads"
    __table_args__ = (
        Index("ix_chat_threads_parent_status", "parent_channel_id", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    guild_id: Mapped[str] = mapped_column(index=True)
    parent_channel_id: Mapped[str] = mapped_column(index=True)
    discord_thread_id: Mapped[str] = mapped_column(unique=True, index=True)
    title: Mapped[str] = mapped_column(Text())
    topic: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(index=True, default="active")
    turn_count: Mapped[int] = mapped_column(Integer(), default=0)
    last_actor_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_message_preview: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatParticipantModel(Base):
    __tablename__ = "chat_participants"
    __table_args__ = (
        UniqueConstraint("thread_id", "actor_name", name="uq_chat_participant_per_thread"),
        Index("ix_chat_participants_thread_last_message", "thread_id", "last_message_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True)
    actor_name: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(Text(), default="participant")
    turn_count: Mapped[int] = mapped_column(Integer(), default=0)
    last_message_preview: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatMessageModel(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_thread_created", "thread_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True)
    actor_name: Mapped[str] = mapped_column(index=True)
    event_kind: Mapped[str] = mapped_column(Text(), default="message")
    content: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
