"""Kernel-level generic approval primitive.

Behaviors that need a "wait for a human or another actor before
proceeding" gate share this primitive instead of each rolling its own
approval table and resolution semantics.

Lifecycle:
    request -> pending -> { approved | rejected | expired }

Decision vocabulary follows codex's superset
(approved / approved_for_session / rejected / abort) so behaviors that
forward codex's typed approval requests don't need to translate. The
kernel itself does not enforce any specific decision name — the
``resolution`` column is freeform for behaviors that want richer states
(e.g. "deferred", "escalated") on top of the base ``status``.

Statelessness: like ``KernelScratchService``, callers pass a SQLAlchemy
``Session`` per call so the service composes with ``session_scope()``
and the existing unit-of-work pattern. The host session is configured
with ``autoflush=False``, so we flush explicitly on writes to keep
within-transaction reads consistent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KernelApprovalModel, utcnow

__all__ = [
    "KernelApprovalService",
    "ApprovalRecord",
    "APPROVAL_STATUS_PENDING",
    "APPROVAL_STATUS_APPROVED",
    "APPROVAL_STATUS_REJECTED",
    "APPROVAL_STATUS_EXPIRED",
    "APPROVAL_TERMINAL_STATUSES",
    "APPROVAL_DECISIONS",
]


APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_REJECTED = "rejected"
APPROVAL_STATUS_EXPIRED = "expired"

APPROVAL_TERMINAL_STATUSES = frozenset(
    {APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED, APPROVAL_STATUS_EXPIRED}
)

# The decision vocabulary that maps onto codex's typed approval params
# (ApplyPatchApproval / ExecCommandApproval / etc.). Behaviors that
# don't need every variant can ignore them; the kernel just stores
# whatever the caller hands in. Each decision implies a status:
APPROVAL_DECISIONS: dict[str, str] = {
    "approved": APPROVAL_STATUS_APPROVED,
    "approved_for_session": APPROVAL_STATUS_APPROVED,
    "rejected": APPROVAL_STATUS_REJECTED,
    "abort": APPROVAL_STATUS_REJECTED,
}


@dataclass(slots=True)
class ApprovalRecord:
    id: str
    space_id: str
    kind: str
    status: str
    payload: dict[str, Any]
    requested_by: str
    requested_at: datetime
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None
    note: str | None = None
    expires_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc_aware(value: datetime | None) -> datetime | None:
    """SQLite drivers can hand back naive datetimes from
    ``DateTime(timezone=True)`` columns. Treat naive values as UTC so
    timedelta arithmetic against ``_now()`` doesn't blow up.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _record_from_row(row: KernelApprovalModel) -> ApprovalRecord:
    try:
        payload = json.loads(row.payload_json or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except (TypeError, ValueError):
        payload = {}
    return ApprovalRecord(
        id=row.id,
        space_id=row.space_id,
        kind=row.kind,
        status=row.status,
        payload=payload,
        requested_by=row.requested_by or "",
        requested_at=row.requested_at,
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
        resolution=row.resolution,
        note=row.note,
        expires_at=row.expires_at,
    )


class KernelApprovalService:
    def request(
        self,
        session: Session,
        *,
        space_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        requested_by: str = "",
        ttl_seconds: int | None = None,
    ) -> ApprovalRecord:
        if not space_id:
            raise ValueError("space_id is required")
        if not kind:
            raise ValueError("kind is required")
        now = utcnow()
        expires_at = (
            now + timedelta(seconds=int(ttl_seconds))
            if ttl_seconds is not None and ttl_seconds > 0
            else None
        )
        row = KernelApprovalModel(
            space_id=str(space_id),
            kind=str(kind),
            payload_json=json.dumps(payload or {}, ensure_ascii=False, default=str),
            status=APPROVAL_STATUS_PENDING,
            requested_by=str(requested_by or ""),
            requested_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        session.flush()
        return _record_from_row(row)

    def get(self, session: Session, *, approval_id: str) -> ApprovalRecord | None:
        row = session.get(KernelApprovalModel, str(approval_id))
        if row is None:
            return None
        if self._is_expired(row):
            self._mark_row_expired(session, row)
            session.flush()
        return _record_from_row(row)

    def resolve(
        self,
        session: Session,
        *,
        approval_id: str,
        resolution: str,
        resolved_by: str = "",
        note: str | None = None,
    ) -> ApprovalRecord | None:
        row = session.get(KernelApprovalModel, str(approval_id))
        if row is None:
            return None
        if self._is_expired(row):
            self._mark_row_expired(session, row)
            session.flush()
            return _record_from_row(row)
        if row.status in APPROVAL_TERMINAL_STATUSES:
            # Already resolved — treat as idempotent: return the existing
            # record without overwriting an earlier resolution.
            return _record_from_row(row)

        normalized = str(resolution or "").strip()
        if not normalized:
            raise ValueError("resolution is required")
        target_status = APPROVAL_DECISIONS.get(normalized.lower())
        if target_status is None:
            # Free-form resolution string is allowed but we still need a
            # base status. Fall back to APPROVED unless the resolution
            # word looks negative.
            negative_markers = ("reject", "denied", "abort", "cancel", "no")
            target_status = (
                APPROVAL_STATUS_REJECTED
                if any(marker in normalized.lower() for marker in negative_markers)
                else APPROVAL_STATUS_APPROVED
            )

        now = utcnow()
        row.status = target_status
        row.resolution = normalized
        row.resolved_by = str(resolved_by or "")
        row.resolved_at = now
        if note is not None:
            row.note = str(note)
        session.flush()
        return _record_from_row(row)

    def list_pending(
        self,
        session: Session,
        *,
        space_id: str,
        kinds: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[ApprovalRecord]:
        statement = (
            select(KernelApprovalModel)
            .where(KernelApprovalModel.space_id == str(space_id))
            .where(KernelApprovalModel.status == APPROVAL_STATUS_PENDING)
            .order_by(KernelApprovalModel.requested_at.asc())
            .limit(max(1, int(limit)))
        )
        kinds_set = {str(k) for k in (kinds or []) if k}
        if kinds_set:
            statement = statement.where(KernelApprovalModel.kind.in_(kinds_set))
        rows = list(session.execute(statement).scalars().all())
        live: list[ApprovalRecord] = []
        for row in rows:
            if self._is_expired(row):
                self._mark_row_expired(session, row)
                continue
            live.append(_record_from_row(row))
        if rows:
            session.flush()
        return live

    def expire_due(self, session: Session, *, batch_size: int = 1000) -> int:
        statement = (
            select(KernelApprovalModel)
            .where(KernelApprovalModel.status == APPROVAL_STATUS_PENDING)
            .where(KernelApprovalModel.expires_at.is_not(None))
            .limit(max(1, int(batch_size)))
        )
        rows = list(session.execute(statement).scalars().all())
        expired = 0
        for row in rows:
            if not self._is_expired(row):
                continue
            self._mark_row_expired(session, row)
            expired += 1
        if expired:
            session.flush()
        return expired

    @staticmethod
    def _is_expired(row: KernelApprovalModel) -> bool:
        if row.status != APPROVAL_STATUS_PENDING:
            return False
        expires_at = _coerce_utc_aware(row.expires_at)
        return expires_at is not None and expires_at <= _now()

    @staticmethod
    def _mark_row_expired(session: Session, row: KernelApprovalModel) -> None:
        row.status = APPROVAL_STATUS_EXPIRED
        row.resolution = row.resolution or "expired"
        row.resolved_at = utcnow()
