"""Kernel-level generic task queue primitive.

Behaviors that need a "queue → claim → execute → complete" lifecycle
share this primitive instead of each one rolling its own table and
claim semantics. Today that pattern is duplicated across orchestration
(session launches / project finds / verification) and remote_codex
(task claims / agent commands), and a new behavior would otherwise have
to invent its own.

Status lifecycle:
    queued → claimed → executing → { completed | failed | cancelled }

Lease semantics: ``claim_next`` atomically transitions the highest-
priority oldest queued task to ``claimed``, stamps it with the calling
actor's id, and issues a lease token. The worker calls ``heartbeat``
periodically with the same token to extend the lease, ``complete`` /
``fail`` to terminate it, or just lets it expire. ``release_expired``
sweeps lapsed leases back to ``queued`` so another worker can pick
them up.

Statelessness: like the other kernel primitives this lives behind a
SQLAlchemy ``Session``-passing service so it composes with the
existing ``session_scope()`` pattern. The host session is configured
``autoflush=False``, so writes flush explicitly to keep within-
transaction reads consistent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KernelTaskModel, utcnow

__all__ = [
    "KernelTaskService",
    "KernelTaskRecord",
    "TaskClaim",
    "TASK_STATUS_QUEUED",
    "TASK_STATUS_CLAIMED",
    "TASK_STATUS_EXECUTING",
    "TASK_STATUS_COMPLETED",
    "TASK_STATUS_FAILED",
    "TASK_STATUS_CANCELLED",
    "TASK_TERMINAL_STATUSES",
    "TASK_ACTIVE_STATUSES",
]


TASK_STATUS_QUEUED = "queued"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_EXECUTING = "executing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

TASK_TERMINAL_STATUSES = frozenset(
    {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED}
)
TASK_ACTIVE_STATUSES = frozenset(
    {TASK_STATUS_QUEUED, TASK_STATUS_CLAIMED, TASK_STATUS_EXECUTING}
)


@dataclass(slots=True)
class KernelTaskRecord:
    id: str
    space_id: str
    kind: str
    status: str
    priority: int
    payload: dict[str, Any]
    requested_by: str
    owner_actor_id: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    claim_count: int
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    parent_task_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(slots=True)
class TaskClaim:
    task: KernelTaskRecord
    lease_token: str
    lease_expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded


def _record_from_row(row: KernelTaskModel) -> KernelTaskRecord:
    payload = _decode_json(row.payload_json)
    if not isinstance(payload, dict):
        payload = {}
    result = _decode_json(row.result_json)
    error = _decode_json(row.error_json)
    return KernelTaskRecord(
        id=row.id,
        space_id=row.space_id,
        kind=row.kind,
        status=row.status,
        priority=int(row.priority or 0),
        payload=payload,
        requested_by=row.requested_by or "",
        owner_actor_id=row.owner_actor_id,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        claim_count=int(row.claim_count or 0),
        result=result if isinstance(result, dict) else None,
        error=error if isinstance(error, dict) else None,
        parent_task_id=row.parent_task_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


class TaskLeaseError(RuntimeError):
    """Raised when a heartbeat / complete / fail call presents a lease
    token that doesn't match the current owner. Behaviors should treat
    this as "the lease was already taken by another worker — abandon
    your local work and re-claim from the queue."
    """


class KernelTaskService:
    def enqueue(
        self,
        session: Session,
        *,
        space_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        requested_by: str = "",
        parent_task_id: str | None = None,
        task_id: str | None = None,
    ) -> KernelTaskRecord:
        if not space_id:
            raise ValueError("space_id is required")
        if not kind:
            raise ValueError("kind is required")
        row_kwargs: dict[str, Any] = {
            "space_id": str(space_id),
            "kind": str(kind),
            "status": TASK_STATUS_QUEUED,
            "priority": int(priority),
            "payload_json": json.dumps(payload or {}, ensure_ascii=False, default=str),
            "requested_by": str(requested_by or ""),
            "parent_task_id": str(parent_task_id) if parent_task_id else None,
        }
        if task_id:
            # Behaviors that mirror an externally-keyed row (e.g. a
            # remote_codex command) reuse its id as the kernel task id
            # so the kernel record stays 1:1 with the legacy row without
            # needing a separate join column.
            row_kwargs["id"] = str(task_id)
        row = KernelTaskModel(**row_kwargs)
        session.add(row)
        session.flush()
        return _record_from_row(row)

    def get(self, session: Session, *, task_id: str) -> KernelTaskRecord | None:
        row = session.get(KernelTaskModel, str(task_id))
        return _record_from_row(row) if row is not None else None

    def claim_next(
        self,
        session: Session,
        *,
        space_id: str | None = None,
        kinds: Iterable[str] | None = None,
        actor_id: str,
        lease_seconds: int = 60,
    ) -> TaskClaim | None:
        """Claim the highest-priority oldest queued task that matches the
        filters, in a single atomic transition. Returns ``None`` when
        the queue is empty.
        """
        if not actor_id:
            raise ValueError("actor_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        # Sweep stale leases first so an abandoned task doesn't sit
        # blocking the queue for the next worker.
        self._release_expired_locked(session)

        statement = (
            select(KernelTaskModel)
            .where(KernelTaskModel.status == TASK_STATUS_QUEUED)
            .order_by(
                KernelTaskModel.priority.desc(),
                KernelTaskModel.created_at.asc(),
            )
            .limit(1)
        )
        if space_id:
            statement = statement.where(KernelTaskModel.space_id == str(space_id))
        kind_set = {str(k) for k in (kinds or []) if k}
        if kind_set:
            statement = statement.where(KernelTaskModel.kind.in_(kind_set))

        row = session.execute(statement).scalar_one_or_none()
        if row is None:
            return None

        now = utcnow()
        lease_token = str(uuid.uuid4())
        lease_expires_at = now + timedelta(seconds=int(lease_seconds))
        row.status = TASK_STATUS_CLAIMED
        row.owner_actor_id = str(actor_id)
        row.lease_token = lease_token
        row.lease_expires_at = lease_expires_at
        row.claim_count = int(row.claim_count or 0) + 1
        row.started_at = row.started_at or now
        row.updated_at = now
        session.flush()
        return TaskClaim(
            task=_record_from_row(row),
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )

    def heartbeat(
        self,
        session: Session,
        *,
        task_id: str,
        lease_token: str,
        lease_seconds: int = 60,
        status: str = TASK_STATUS_EXECUTING,
    ) -> KernelTaskRecord:
        if status not in (TASK_STATUS_CLAIMED, TASK_STATUS_EXECUTING):
            raise ValueError("heartbeat status must be claimed or executing")
        row = self._require_owned_row(session, task_id=task_id, lease_token=lease_token)
        now = utcnow()
        row.status = status
        row.lease_expires_at = now + timedelta(seconds=int(lease_seconds))
        row.updated_at = now
        session.flush()
        return _record_from_row(row)

    def complete(
        self,
        session: Session,
        *,
        task_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
    ) -> KernelTaskRecord:
        row = self._require_owned_row(session, task_id=task_id, lease_token=lease_token)
        now = utcnow()
        row.status = TASK_STATUS_COMPLETED
        row.result_json = json.dumps(result or {}, ensure_ascii=False, default=str)
        row.completed_at = now
        row.lease_expires_at = None
        row.updated_at = now
        session.flush()
        return _record_from_row(row)

    def fail(
        self,
        session: Session,
        *,
        task_id: str,
        lease_token: str,
        error: dict[str, Any] | None = None,
    ) -> KernelTaskRecord:
        row = self._require_owned_row(session, task_id=task_id, lease_token=lease_token)
        now = utcnow()
        row.status = TASK_STATUS_FAILED
        row.error_json = json.dumps(error or {}, ensure_ascii=False, default=str)
        row.completed_at = now
        row.lease_expires_at = None
        row.updated_at = now
        session.flush()
        return _record_from_row(row)

    def cancel(self, session: Session, *, task_id: str, reason: str | None = None) -> KernelTaskRecord | None:
        row = session.get(KernelTaskModel, str(task_id))
        if row is None:
            return None
        if row.status in TASK_TERMINAL_STATUSES:
            return _record_from_row(row)
        now = utcnow()
        row.status = TASK_STATUS_CANCELLED
        row.error_json = json.dumps(
            {"kind": "cancelled", "reason": str(reason) if reason else None},
            ensure_ascii=False,
        )
        row.completed_at = now
        row.lease_expires_at = None
        row.updated_at = now
        session.flush()
        return _record_from_row(row)

    def list(
        self,
        session: Session,
        *,
        space_id: str | None = None,
        kinds: Iterable[str] | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[KernelTaskRecord]:
        statement = (
            select(KernelTaskModel)
            .order_by(
                KernelTaskModel.created_at.asc(),
            )
            .limit(max(1, int(limit)))
        )
        if space_id:
            statement = statement.where(KernelTaskModel.space_id == str(space_id))
        kind_set = {str(k) for k in (kinds or []) if k}
        if kind_set:
            statement = statement.where(KernelTaskModel.kind.in_(kind_set))
        status_set = {str(s) for s in (statuses or []) if s}
        if status_set:
            statement = statement.where(KernelTaskModel.status.in_(status_set))
        rows = list(session.execute(statement).scalars().all())
        return [_record_from_row(row) for row in rows]

    def release_expired_leases(self, session: Session, *, batch_size: int = 1000) -> int:
        """Public wrapper around the private sweep used inside
        ``claim_next``. Behaviors that run a periodic sweep can call
        this directly; ``claim_next`` calls it lazily on every claim
        attempt so a single worker pool stays healthy without an
        external scheduler.
        """
        return self._release_expired_locked(session, batch_size=batch_size)

    def _release_expired_locked(self, session: Session, *, batch_size: int = 1000) -> int:
        statement = (
            select(KernelTaskModel)
            .where(KernelTaskModel.status.in_((TASK_STATUS_CLAIMED, TASK_STATUS_EXECUTING)))
            .where(KernelTaskModel.lease_expires_at.is_not(None))
            .limit(max(1, int(batch_size)))
        )
        rows = list(session.execute(statement).scalars().all())
        now = _now()
        released = 0
        for row in rows:
            expires_at = _coerce_utc_aware(row.lease_expires_at)
            if expires_at is None or expires_at > now:
                continue
            row.status = TASK_STATUS_QUEUED
            row.owner_actor_id = None
            row.lease_token = None
            row.lease_expires_at = None
            row.updated_at = utcnow()
            released += 1
        if released:
            session.flush()
        return released

    @staticmethod
    def _require_owned_row(
        session: Session,
        *,
        task_id: str,
        lease_token: str,
    ) -> KernelTaskModel:
        row = session.get(KernelTaskModel, str(task_id))
        if row is None:
            raise TaskLeaseError(f"unknown task: {task_id}")
        if row.lease_token != lease_token:
            raise TaskLeaseError(f"lease mismatch on task {task_id}")
        return row
