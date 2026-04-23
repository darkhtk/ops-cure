"""Generic presence and lease primitives reusable across behaviors."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from ..db import session_scope
from ..models import ActorSessionModel, ResourceLeaseModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@contextmanager
def _db_scope(db=None):
    if db is not None:
        yield db
        return
    with session_scope() as managed_db:
        yield managed_db


class ActorSessionSummary(BaseModel):
    session_id: str
    actor_id: str
    scope_kind: str
    scope_id: str
    status: str
    last_seen_at: datetime
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ScopePresenceResponse(BaseModel):
    scope_kind: str
    scope_id: str
    sessions: list[ActorSessionSummary] = Field(default_factory=list)


class ActorSessionUpsertRequest(BaseModel):
    session_id: str | None = None
    actor_id: str
    scope_kind: str
    scope_id: str
    status: str = "active"
    ttl_seconds: int = 120

    @field_validator("ttl_seconds")
    @classmethod
    def validate_ttl_seconds(cls, value: int) -> int:
        return max(10, min(value, 3600))


class ResourceLeaseSummary(BaseModel):
    lease_id: str
    resource_kind: str
    resource_id: str
    holder_actor_id: str
    lease_token: str
    claimed_at: datetime
    expires_at: datetime
    status: str
    released_at: datetime | None = None
    updated_at: datetime


class ResourceLeaseClaimRequest(BaseModel):
    resource_kind: str
    resource_id: str
    holder_actor_id: str
    lease_token: str | None = None
    lease_seconds: int = 120
    status: str = "claimed"

    @field_validator("lease_seconds")
    @classmethod
    def validate_lease_seconds(cls, value: int) -> int:
        return max(10, min(value, 3600))


class ResourceLeaseHeartbeatRequest(BaseModel):
    holder_actor_id: str
    lease_token: str
    lease_seconds: int = 120
    status: str | None = None

    @field_validator("lease_seconds")
    @classmethod
    def validate_lease_seconds(cls, value: int) -> int:
        return max(10, min(value, 3600))


class ResourceLeaseReleaseRequest(BaseModel):
    holder_actor_id: str
    lease_token: str
    status: str = "released"


class PresenceService:
    def upsert_actor_session(self, payload: ActorSessionUpsertRequest, *, db=None) -> ActorSessionSummary:
        now = utcnow()
        with _db_scope(db) as active_db:
            row = None
            if payload.session_id:
                row = active_db.scalar(select(ActorSessionModel).where(ActorSessionModel.id == payload.session_id))
            if row is None:
                row = ActorSessionModel(
                    id=payload.session_id or str(uuid.uuid4()),
                    actor_id=payload.actor_id,
                    scope_kind=payload.scope_kind,
                    scope_id=payload.scope_id,
                    created_at=now,
                )
                active_db.add(row)
            row.actor_id = payload.actor_id
            row.scope_kind = payload.scope_kind
            row.scope_id = payload.scope_id
            row.status = payload.status
            row.last_seen_at = now
            row.expires_at = now + timedelta(seconds=payload.ttl_seconds)
            active_db.flush()
            active_db.refresh(row)
            return self._to_actor_session_summary(row)

    def list_presence(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        active_only: bool = True,
        db=None,
    ) -> ScopePresenceResponse:
        now = utcnow()
        with _db_scope(db) as active_db:
            query = (
                select(ActorSessionModel)
                .where(ActorSessionModel.scope_kind == scope_kind)
                .where(ActorSessionModel.scope_id == scope_id)
                .order_by(ActorSessionModel.last_seen_at.desc(), ActorSessionModel.created_at.desc())
            )
            rows = list(active_db.scalars(query))
            if active_only:
                rows = [
                    row
                    for row in rows
                    if ensure_utc(row.expires_at) > now and row.status not in {"closed", "expired"}
                ]
            return ScopePresenceResponse(
                scope_kind=scope_kind,
                scope_id=scope_id,
                sessions=[self._to_actor_session_summary(row) for row in rows],
            )

    def claim_resource_lease(self, payload: ResourceLeaseClaimRequest, *, db=None) -> ResourceLeaseSummary:
        now = utcnow()
        with _db_scope(db) as active_db:
            current = self._get_current_lease_row(
                db=active_db,
                resource_kind=payload.resource_kind,
                resource_id=payload.resource_id,
            )
            if current is not None:
                if current.holder_actor_id != payload.holder_actor_id:
                    raise ValueError(
                        f"Resource `{payload.resource_kind}:{payload.resource_id}` is already held by "
                        f"`{current.holder_actor_id}`.",
                    )
                current.lease_token = payload.lease_token or current.lease_token
                current.expires_at = now + timedelta(seconds=payload.lease_seconds)
                current.status = payload.status
                current.updated_at = now
                active_db.flush()
                active_db.refresh(current)
                return self._to_resource_lease_summary(current)

            stale = self._get_latest_lease_row(
                db=active_db,
                resource_kind=payload.resource_kind,
                resource_id=payload.resource_id,
            )
            if stale is not None and stale.released_at is None and ensure_utc(stale.expires_at) <= now:
                stale.status = "expired"
                stale.updated_at = now

            row = ResourceLeaseModel(
                resource_kind=payload.resource_kind,
                resource_id=payload.resource_id,
                holder_actor_id=payload.holder_actor_id,
                lease_token=payload.lease_token or str(uuid.uuid4()),
                claimed_at=now,
                expires_at=now + timedelta(seconds=payload.lease_seconds),
                status=payload.status,
                updated_at=now,
            )
            active_db.add(row)
            active_db.flush()
            active_db.refresh(row)
            return self._to_resource_lease_summary(row)

    def get_current_lease(self, *, resource_kind: str, resource_id: str, db=None) -> ResourceLeaseSummary | None:
        with _db_scope(db) as active_db:
            row = self._get_current_lease_row(
                db=active_db,
                resource_kind=resource_kind,
                resource_id=resource_id,
            )
            if row is None:
                return None
            return self._to_resource_lease_summary(row)

    def heartbeat_resource_lease(
        self,
        *,
        lease_id: str,
        payload: ResourceLeaseHeartbeatRequest,
        db=None,
    ) -> ResourceLeaseSummary:
        now = utcnow()
        with _db_scope(db) as active_db:
            row = self._require_lease(db=active_db, lease_id=lease_id)
            self._validate_lease_holder(row=row, holder_actor_id=payload.holder_actor_id, lease_token=payload.lease_token)
            if row.released_at is not None or row.status == "released":
                raise ValueError(f"Lease `{lease_id}` has already been released.")
            if ensure_utc(row.expires_at) <= now:
                raise ValueError(f"Lease `{lease_id}` has expired.")
            row.expires_at = now + timedelta(seconds=payload.lease_seconds)
            if payload.status:
                row.status = payload.status
            row.updated_at = now
            active_db.flush()
            active_db.refresh(row)
            return self._to_resource_lease_summary(row)

    def release_resource_lease(
        self,
        *,
        lease_id: str,
        payload: ResourceLeaseReleaseRequest,
        db=None,
    ) -> ResourceLeaseSummary:
        now = utcnow()
        with _db_scope(db) as active_db:
            row = self._require_lease(db=active_db, lease_id=lease_id)
            self._validate_lease_holder(row=row, holder_actor_id=payload.holder_actor_id, lease_token=payload.lease_token)
            row.status = payload.status
            row.released_at = now
            row.expires_at = now
            row.updated_at = now
            active_db.flush()
            active_db.refresh(row)
            return self._to_resource_lease_summary(row)

    @staticmethod
    def _require_lease(*, db, lease_id: str) -> ResourceLeaseModel:
        row = db.scalar(select(ResourceLeaseModel).where(ResourceLeaseModel.id == lease_id))
        if row is None:
            raise ValueError(f"Lease `{lease_id}` was not found.")
        return row

    @staticmethod
    def _validate_lease_holder(*, row: ResourceLeaseModel, holder_actor_id: str, lease_token: str) -> None:
        if row.holder_actor_id != holder_actor_id:
            raise ValueError(
                f"Lease `{row.id}` is held by `{row.holder_actor_id}`, not `{holder_actor_id}`.",
            )
        if row.lease_token != lease_token:
            raise ValueError(f"Lease token does not match lease `{row.id}`.")

    @staticmethod
    def _get_current_lease_row(*, db, resource_kind: str, resource_id: str) -> ResourceLeaseModel | None:
        now = utcnow()
        rows = list(
            db.scalars(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.resource_kind == resource_kind)
                .where(ResourceLeaseModel.resource_id == resource_id)
                .order_by(ResourceLeaseModel.claimed_at.desc(), ResourceLeaseModel.updated_at.desc()),
            ),
        )
        for row in rows:
            if row.released_at is None and ensure_utc(row.expires_at) > now and row.status != "expired":
                return row
        return None

    @staticmethod
    def _get_latest_lease_row(*, db, resource_kind: str, resource_id: str) -> ResourceLeaseModel | None:
        return db.scalar(
            select(ResourceLeaseModel)
            .where(ResourceLeaseModel.resource_kind == resource_kind)
            .where(ResourceLeaseModel.resource_id == resource_id)
            .order_by(ResourceLeaseModel.claimed_at.desc(), ResourceLeaseModel.updated_at.desc()),
        )

    @staticmethod
    def _to_actor_session_summary(row: ActorSessionModel) -> ActorSessionSummary:
        return ActorSessionSummary(
            session_id=row.id,
            actor_id=row.actor_id,
            scope_kind=row.scope_kind,
            scope_id=row.scope_id,
            status=row.status,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_resource_lease_summary(row: ResourceLeaseModel) -> ResourceLeaseSummary:
        return ResourceLeaseSummary(
            lease_id=row.id,
            resource_kind=row.resource_kind,
            resource_id=row.resource_id,
            holder_actor_id=row.holder_actor_id,
            lease_token=row.lease_token,
            claimed_at=row.claimed_at,
            expires_at=row.expires_at,
            status=row.status,
            released_at=row.released_at,
            updated_at=row.updated_at,
        )
