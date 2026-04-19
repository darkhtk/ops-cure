from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    project_name: Mapped[str] = mapped_column(index=True)
    preset: Mapped[str | None] = mapped_column(nullable=True)
    discord_thread_id: Mapped[str] = mapped_column(unique=True, index=True)
    guild_id: Mapped[str] = mapped_column(index=True)
    parent_channel_id: Mapped[str] = mapped_column(index=True)
    workdir: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(index=True, default="waiting_for_workers")
    created_by: Mapped[str] = mapped_column(index=True)
    launcher_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    send_ready_message: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agents: Mapped[list["AgentModel"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list["JobModel"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    transcripts: Mapped[list["TranscriptModel"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    project_finds: Mapped[list["ProjectFindModel"]] = relationship(
        back_populates="session",
    )


class AgentModel(Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("session_id", "agent_name", name="uq_agent_per_session"),
        Index("ix_agents_session_status", "session_id", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    agent_name: Mapped[str] = mapped_column(index=True)
    cli_type: Mapped[str] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(Text())
    is_default: Mapped[bool] = mapped_column(Boolean(), default=False)
    status: Mapped[str] = mapped_column(index=True, default="offline")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pid_hint: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    session: Mapped[SessionModel] = relationship(back_populates="agents")


class JobModel(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_agent_status_created", "session_id", "agent_name", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    agent_name: Mapped[str] = mapped_column(index=True)
    job_type: Mapped[str] = mapped_column(index=True, default="message")
    source_discord_message_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(index=True)
    input_text: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(index=True, default="pending")
    worker_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    result_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[SessionModel] = relationship(back_populates="jobs")


class TranscriptModel(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(index=True)
    content: Mapped[str] = mapped_column(Text())
    source_discord_message_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[SessionModel] = relationship(back_populates="transcripts")


class ProjectFindModel(Base):
    __tablename__ = "project_finds"
    __table_args__ = (
        Index("ix_project_finds_status_created", "status", "created_at"),
        Index("ix_project_finds_preset_status", "preset", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    preset: Mapped[str] = mapped_column(index=True)
    query_text: Mapped[str] = mapped_column(Text())
    requested_by: Mapped[str] = mapped_column(index=True)
    guild_id: Mapped[str] = mapped_column(index=True)
    parent_channel_id: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(index=True, default="pending")
    launcher_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    selected_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    selected_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float(), nullable=True)
    candidates_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    discord_thread_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[SessionModel | None] = relationship(back_populates="project_finds")
