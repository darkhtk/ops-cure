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
    power_target_name: Mapped[str | None] = mapped_column(index=True, nullable=True)
    execution_target_name: Mapped[str | None] = mapped_column(index=True, nullable=True)
    discord_thread_id: Mapped[str] = mapped_column(unique=True, index=True)
    guild_id: Mapped[str] = mapped_column(index=True)
    parent_channel_id: Mapped[str] = mapped_column(index=True)
    workdir: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(index=True, default="waiting_for_workers")
    desired_status: Mapped[str] = mapped_column(index=True, default="ready")
    power_state: Mapped[str] = mapped_column(index=True, default="unknown")
    execution_state: Mapped[str] = mapped_column(index=True, default="unknown")
    pause_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_recovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_recovery_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer(), default=1)
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
    policies: Mapped[list["SessionPolicyModel"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    operations: Mapped[list["SessionOperationModel"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
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
    desired_status: Mapped[str] = mapped_column(index=True, default="ready")
    paused_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
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


class PowerTargetModel(Base):
    __tablename__ = "power_targets"
    __table_args__ = (UniqueConstraint("name", name="uq_power_target_name"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(index=True, default="noop")
    mac_address: Mapped[str | None] = mapped_column(Text(), nullable=True)
    broadcast_ip: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExecutionTargetModel(Base):
    __tablename__ = "execution_targets"
    __table_args__ = (UniqueConstraint("name", name="uq_execution_target_name"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(index=True, default="windows_launcher")
    platform: Mapped[str] = mapped_column(index=True, default="windows")
    launcher_id_hint: Mapped[str | None] = mapped_column(Text(), nullable=True)
    host_pattern: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SessionPolicyModel(Base):
    __tablename__ = "session_policies"
    __table_args__ = (UniqueConstraint("session_id", name="uq_session_policy_session"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(index=True, default="preset")
    policy_json: Mapped[str] = mapped_column(Text())
    version: Mapped[int] = mapped_column(Integer(), default=1)
    updated_by: Mapped[str] = mapped_column(index=True, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    session: Mapped[SessionModel] = relationship(back_populates="policies")


class SessionOperationModel(Base):
    __tablename__ = "session_operations"
    __table_args__ = (
        Index("ix_session_operations_session_status", "session_id", "status"),
        Index("ix_session_operations_type_status", "operation_type", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    operation_type: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(index=True, default="pending")
    requested_by: Mapped[str] = mapped_column(index=True)
    input_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[SessionModel] = relationship(back_populates="operations")
