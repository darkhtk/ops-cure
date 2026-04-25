"""Kernel-level generic scratch store.

Behaviors and runtimes routinely need a tiny key-value side-channel — dedup
markers, last-seen sequence numbers, "did I already process this command",
rate-limit counters, ephemeral capability flags. Today every such need is
answered by a one-off JSON file, a per-feature SQLite column, or a
per-behavior table. This module is the shared primitive that replaces those
ad-hoc patterns.

Scope rules:
- ``actor_id``: empty string for behavior-global keys, otherwise the actor
  this entry belongs to (typically a launcher / device id).
- ``space_id``: empty string for actor-global keys, otherwise the space
  (thread / room / session) this entry belongs to.
- ``key``: the caller-defined namespace; conventionally
  ``"<behavior>.<feature>.<id>"`` so two callers don't accidentally collide.

TTL is optional. ``cleanup_expired`` should be called periodically by the
host to drop dead rows. ``get`` already filters expired entries even if
they have not been deleted yet, so callers never see stale state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KernelScratchModel, utcnow

__all__ = ["KernelScratchService"]


_SENTINEL = object()


def _normalize_str(value: Any) -> str:
    return "" if value is None else str(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc_aware(value: datetime | None) -> datetime | None:
    """Some SQLite drivers return naive datetimes even when the column is
    declared with ``DateTime(timezone=True)``. Treat any naive value the
    ORM hands us as UTC so we can compare against ``_now()`` safely.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class KernelScratchService:
    """Read/write façade for the ``kernel_scratch`` table.

    The service is intentionally stateless. Callers pass a SQLAlchemy
    ``Session`` per call so this composes cleanly with the existing
    ``session_scope()`` context manager and unit-of-work patterns the rest
    of nas_bridge already uses.
    """

    def get(
        self,
        session: Session,
        *,
        key: str,
        actor_id: str = "",
        space_id: str = "",
        default: Any = None,
    ) -> Any:
        """Return the stored value for ``(actor_id, space_id, key)``.

        Expired entries are treated as missing — they are not returned and
        not touched here (cleanup happens via ``cleanup_expired``).
        """
        row = self._fetch_row(session, actor_id=actor_id, space_id=space_id, key=key)
        if row is None:
            return default
        expires_at = _coerce_utc_aware(row.expires_at)
        if expires_at is not None and expires_at <= _now():
            return default
        try:
            return json.loads(row.value_json)
        except (TypeError, ValueError):
            return default

    def has(
        self,
        session: Session,
        *,
        key: str,
        actor_id: str = "",
        space_id: str = "",
    ) -> bool:
        sentinel = _SENTINEL
        return self.get(
            session,
            key=key,
            actor_id=actor_id,
            space_id=space_id,
            default=sentinel,
        ) is not sentinel

    def set(
        self,
        session: Session,
        *,
        key: str,
        value: Any,
        actor_id: str = "",
        space_id: str = "",
        ttl_seconds: int | None = None,
    ) -> None:
        """Insert or update the entry for ``(actor_id, space_id, key)``.

        The value is JSON-serialized; pass anything ``json.dumps`` accepts.
        ``ttl_seconds`` is interpreted relative to the current UTC time;
        pass ``None`` for non-expiring entries.
        """
        now = utcnow()
        expires_at: datetime | None = None
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                # A non-positive TTL means "store as already expired" — we
                # honor it as a delete-equivalent so callers can ergonomically
                # purge entries through the same code path.
                self.delete(session, key=key, actor_id=actor_id, space_id=space_id)
                return
            expires_at = now + timedelta(seconds=int(ttl_seconds))

        encoded = json.dumps(value, ensure_ascii=False, default=str)
        actor = _normalize_str(actor_id)
        space = _normalize_str(space_id)

        row = self._fetch_row(session, actor_id=actor, space_id=space, key=key)
        if row is None:
            row = KernelScratchModel(
                actor_id=actor,
                space_id=space,
                key=str(key),
                value_json=encoded,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
            session.add(row)
            # The host session is configured with autoflush=False, so we must
            # flush here ourselves: without it, two consecutive set() calls in
            # the same unit-of-work both try to INSERT and the second one
            # trips the (actor_id, space_id, key) unique constraint instead
            # of finding the first row to UPDATE in place.
            session.flush()
            return

        row.value_json = encoded
        row.updated_at = now
        row.expires_at = expires_at
        session.flush()

    def delete(
        self,
        session: Session,
        *,
        key: str,
        actor_id: str = "",
        space_id: str = "",
    ) -> bool:
        row = self._fetch_row(session, actor_id=actor_id, space_id=space_id, key=key)
        if row is None:
            return False
        session.delete(row)
        return True

    def cleanup_expired(self, session: Session, *, batch_size: int = 1000) -> int:
        """Delete rows whose ``expires_at`` is in the past.

        Returns the number of rows removed. Designed to be called from a
        periodic kernel-side maintenance task; safe to call concurrently
        with reads since each scratch row is independent.
        """
        now = _now()
        statement = (
            select(KernelScratchModel)
            .where(KernelScratchModel.expires_at.is_not(None))
            .limit(max(1, int(batch_size)))
        )
        rows: Iterable[KernelScratchModel] = session.execute(statement).scalars().all()
        removed = 0
        for row in rows:
            expires_at = _coerce_utc_aware(row.expires_at)
            if expires_at is None or expires_at > now:
                continue
            session.delete(row)
            removed += 1
        return removed

    @staticmethod
    def _fetch_row(
        session: Session,
        *,
        actor_id: str,
        space_id: str,
        key: str,
    ) -> KernelScratchModel | None:
        statement = (
            select(KernelScratchModel)
            .where(KernelScratchModel.actor_id == _normalize_str(actor_id))
            .where(KernelScratchModel.space_id == _normalize_str(space_id))
            .where(KernelScratchModel.key == str(key))
            .limit(1)
        )
        return session.execute(statement).scalar_one_or_none()
