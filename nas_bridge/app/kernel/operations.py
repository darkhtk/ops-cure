"""Kernel-level Operation primitive.

Two layers live in this module:

1. **SQLAlchemy persistence models** (``OperationModel`` and friends).
   These were promoted from the product-layer ``RemoteTaskModel``
   (Candidate 2 in ``docs/generic-kernel-promotion-candidates.md``).
   The shape — ownership / progress / evidence / approval / notes
   lifecycle — is unchanged. The existing ``app.models.RemoteTaskModel``
   names continue to work as Python aliases so call sites and tests
   stay untouched. ``__tablename__`` values are also unchanged
   (``remote_tasks``, ``remote_task_assignments``, etc.) so no DB
   migration is needed.

2. **Pydantic shape sketches** (``OperationSummary`` and friends),
   originally landed as a forward-looking draft of the kernel
   vocabulary. They are not wired into persistence yet; the
   product-layer ``RemoteTaskSummaryResponse`` is still the response
   shape used over the wire. These sketches are kept here as a
   target for a future API surface unification.

The ``machine_id`` column (currently overloaded with sentinels like
``"chat"``) is the next promotion target — collapsing it into a generic
``space_id`` so chat / remote_codex / remote_claude / future behaviors
share one scope vocabulary. Deferred to a follow-up PR.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- SQLAlchemy persistence models -----------------------------------------


class OperationModel(Base):
    __tablename__ = "remote_tasks"
    __table_args__ = (
        Index("ix_remote_tasks_machine_status_created", "machine_id", "status", "created_at"),
        Index("ix_remote_tasks_thread_status_updated", "thread_id", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    machine_id: Mapped[str] = mapped_column(index=True)
    thread_id: Mapped[str] = mapped_column(index=True)
    origin_surface: Mapped[str] = mapped_column(index=True, default="browser")
    origin_message_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    objective: Mapped[str] = mapped_column(Text())
    success_criteria_json: Mapped[str] = mapped_column(Text(), default="{}")
    status: Mapped[str] = mapped_column(index=True, default="queued")
    priority: Mapped[str] = mapped_column(index=True, default="normal")
    owner_actor_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    created_by: Mapped[str] = mapped_column(index=True, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    assignments: Mapped[list["OperationAssignmentModel"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    heartbeats: Mapped[list["OperationHeartbeatModel"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    evidence_items: Mapped[list["OperationEvidenceModel"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    approvals: Mapped[list["OperationApprovalModel"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    notes: Mapped[list["OperationNoteModel"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )


class OperationAssignmentModel(Base):
    __tablename__ = "remote_task_assignments"
    __table_args__ = (
        Index("ix_remote_task_assignments_task_status_claimed", "task_id", "status", "claimed_at"),
        Index("ix_remote_task_assignments_actor_status", "actor_id", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("remote_tasks.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str] = mapped_column(index=True)
    lease_token: Mapped[str] = mapped_column(index=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(index=True, default="claimed")
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[OperationModel] = relationship(back_populates="assignments")


class OperationHeartbeatModel(Base):
    __tablename__ = "remote_task_heartbeats"
    __table_args__ = (
        Index("ix_remote_task_heartbeats_task_created", "task_id", "created_at"),
        Index("ix_remote_task_heartbeats_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("remote_tasks.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str] = mapped_column(index=True)
    phase: Mapped[str] = mapped_column(index=True, default="claimed")
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    commands_run_count: Mapped[int] = mapped_column(Integer(), default=0)
    files_read_count: Mapped[int] = mapped_column(Integer(), default=0)
    files_modified_count: Mapped[int] = mapped_column(Integer(), default=0)
    tests_run_count: Mapped[int] = mapped_column(Integer(), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[OperationModel] = relationship(back_populates="heartbeats")


class OperationEvidenceModel(Base):
    __tablename__ = "remote_task_evidence"
    __table_args__ = (
        Index("ix_remote_task_evidence_task_created", "task_id", "created_at"),
        Index("ix_remote_task_evidence_actor_kind", "actor_id", "kind"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("remote_tasks.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(index=True)
    summary: Mapped[str] = mapped_column(Text())
    payload_json: Mapped[str] = mapped_column(Text(), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[OperationModel] = relationship(back_populates="evidence_items")


class OperationApprovalModel(Base):
    __tablename__ = "remote_task_approvals"
    __table_args__ = (
        Index("ix_remote_task_approvals_task_created", "task_id", "requested_at"),
        Index("ix_remote_task_approvals_status_requested", "status", "requested_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("remote_tasks.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str] = mapped_column(index=True)
    reason: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(index=True, default="pending")
    note: Mapped[str | None] = mapped_column(Text(), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(index=True, nullable=True)
    resolution: Mapped[str | None] = mapped_column(index=True, nullable=True)

    task: Mapped[OperationModel] = relationship(back_populates="approvals")


class OperationNoteModel(Base):
    __tablename__ = "remote_task_notes"
    __table_args__ = (
        Index("ix_remote_task_notes_task_created", "task_id", "created_at"),
        Index("ix_remote_task_notes_actor_kind", "actor_id", "kind"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("remote_tasks.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(index=True, default="note")
    content: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[OperationModel] = relationship(back_populates="notes")


# ---- Pydantic shape sketches (forward-looking, not yet wired) --------------


OperationStatus = Literal[
    "queued",
    "claimed",
    "executing",
    "verifying",
    "blocked",
    "interrupted",
    "completed",
    "failed",
    "stalled",
]


class OperationSummary(BaseModel):
    operation_id: str
    space_id: str | None = None
    subject_kind: str
    subject_id: str
    kind: str
    objective: str
    requested_by: str | None = None
    status: OperationStatus = "queued"
    created_at: datetime
    updated_at: datetime


class OperationAssignmentSummary(BaseModel):
    operation_id: str
    actor_id: str
    lease_id: str | None = None
    status: str = "claimed"
    claimed_at: datetime
    released_at: datetime | None = None


class OperationHeartbeatSummary(BaseModel):
    operation_id: str
    actor_id: str
    phase: str
    summary: str | None = None
    metrics: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    created_at: datetime


class ArtifactRefSummary(BaseModel):
    kind: str
    uri: str
    label: str | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class OperationEvidenceSummary(BaseModel):
    operation_id: str
    actor_id: str
    kind: str
    summary: str
    artifact: ArtifactRefSummary | None = None
    created_at: datetime
