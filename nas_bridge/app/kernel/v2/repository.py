"""Repository for protocol v2 — bare-metal CRUD on the v2 tables.

Deliberately thin. No business rules, no state-machine validation,
no event publishing. The next phases (F2-F4) build a service layer
on top that adds invariants, dual-writes, and broker integration.

The Repository takes a SQLAlchemy Session per call rather than
opening its own session_scope -- callers compose with their own
transaction boundaries (typical pattern: ``with session_scope() as
db: repo.insert_actor(db, ...)``).

Key invariants enforced here at the data-access layer:

- ``OperationEventV2Model.seq`` is monotonic per operation. Repository's
  ``insert_event`` does ``SELECT MAX(seq) + 1 FROM events WHERE
  operation_id=...`` inside the same transaction. SQLite's
  serializable default + the (operation_id, seq) UNIQUE makes
  duplicate seq impossible -- a concurrent writer either wins the
  insert or hits the UNIQUE and retries. Actual retry policy is the
  service layer's call (F3+).

- JSON columns get encoded/decoded centrally so callers pass plain
  Python (list/dict) and read plain Python.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    ActorV2Model,
    OperationArtifactV2Model,
    OperationEventV2Model,
    OperationParticipantV2Model,
    OperationV2Model,
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


class V2Repository:
    """All v2 reads/writes go through this. Used by the future v2
    service layer; F1 only proves the schema works end-to-end."""

    # ---------------- actors ----------------

    def insert_actor(
        self,
        db: Session,
        *,
        handle: str,
        display_name: str,
        kind: str = "ai",
        capabilities: list[str] | None = None,
        public_key: bytes | None = None,
        status: str = "offline",
    ) -> ActorV2Model:
        row = ActorV2Model(
            handle=handle,
            display_name=display_name,
            kind=kind,
            capabilities_json=_dumps(capabilities or []),
            public_key=public_key,
            status=status,
        )
        db.add(row)
        db.flush()
        return row

    def get_actor_by_handle(self, db: Session, handle: str) -> ActorV2Model | None:
        return db.scalar(select(ActorV2Model).where(ActorV2Model.handle == handle))

    def update_actor_presence(
        self,
        db: Session,
        *,
        actor_id: str,
        status: str,
        last_seen_at: datetime,
    ) -> ActorV2Model | None:
        row = db.get(ActorV2Model, actor_id)
        if row is None:
            return None
        row.status = status
        row.last_seen_at = last_seen_at
        db.flush()
        return row

    def actor_capabilities(self, row: ActorV2Model) -> list[str]:
        return list(_loads(row.capabilities_json, []))

    # ---------------- operations ----------------

    def insert_operation(
        self,
        db: Session,
        *,
        space_id: str,
        kind: str,
        title: str,
        intent: str | None = None,
        parent_operation_id: str | None = None,
        deadline_at: datetime | None = None,
        on_deadline_action: str | None = None,
        metadata: dict[str, Any] | None = None,
        state: str = "open",
    ) -> OperationV2Model:
        row = OperationV2Model(
            space_id=space_id,
            kind=kind,
            title=title,
            intent=intent,
            parent_operation_id=parent_operation_id,
            deadline_at=deadline_at,
            on_deadline_action=on_deadline_action,
            metadata_json=_dumps(metadata or {}),
            state=state,
        )
        db.add(row)
        db.flush()
        return row

    def get_operation(self, db: Session, operation_id: str) -> OperationV2Model | None:
        return db.get(OperationV2Model, operation_id)

    def operation_metadata(self, row: OperationV2Model) -> dict[str, Any]:
        return dict(_loads(row.metadata_json, {}))

    def operation_policy(self, row: OperationV2Model) -> dict[str, Any]:
        """Return the v3-additive policy dict for the op, or the
        contract default. Stored under metadata.policy for compatibility
        with the existing schema (no v3 column migration needed)."""
        from . import contract as _v2_contract
        meta = self.operation_metadata(row)
        return _v2_contract.validate_operation_policy(meta.get("policy"))

    def set_operation_policy(
        self,
        db: Session,
        *,
        operation_id: str,
        policy: dict[str, Any],
    ) -> OperationV2Model | None:
        """Persist a normalized policy dict into op.metadata.policy.
        Caller is expected to have validated via
        contract.validate_operation_policy first."""
        row = db.get(OperationV2Model, operation_id)
        if row is None:
            return None
        meta = self.operation_metadata(row)
        meta["policy"] = policy
        row.metadata_json = _dumps(meta)
        db.flush()
        return row

    def list_operations_in_space(
        self,
        db: Session,
        *,
        space_id: str,
        kind: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[OperationV2Model]:
        stmt = select(OperationV2Model).where(OperationV2Model.space_id == space_id)
        if kind is not None:
            stmt = stmt.where(OperationV2Model.kind == kind)
        if state is not None:
            stmt = stmt.where(OperationV2Model.state == state)
        stmt = stmt.order_by(OperationV2Model.created_at.desc()).limit(max(1, min(int(limit), 1000)))
        return list(db.scalars(stmt))

    def transition_operation_state(
        self,
        db: Session,
        *,
        operation_id: str,
        to_state: str,
    ) -> "OperationV2Model | None":
        """Move an operation to a new non-terminal state. Caller is
        responsible for validating the transition with
        OperationStateMachine.assert_transition first; this is the
        write-only repo half.

        Does not flip ``state=closed`` -- use ``close_operation`` for
        that since it carries resolution semantics.
        """
        row = db.get(OperationV2Model, operation_id)
        if row is None:
            return None
        row.state = to_state
        db.flush()
        return row

    def close_operation(
        self,
        db: Session,
        *,
        operation_id: str,
        closed_by_actor_id: str | None,
        resolution: str,
        resolution_summary: str | None = None,
        closed_at: datetime,
    ) -> OperationV2Model | None:
        row = db.get(OperationV2Model, operation_id)
        if row is None:
            return None
        row.state = "closed"
        row.resolution = resolution
        row.resolution_summary = resolution_summary
        row.closed_by_actor_id = closed_by_actor_id
        row.closed_at = closed_at
        db.flush()
        return row

    # ---------------- participants ----------------

    def add_participant(
        self,
        db: Session,
        *,
        operation_id: str,
        actor_id: str,
        role: str,
        expected_response_by: datetime | None = None,
    ) -> OperationParticipantV2Model:
        row = OperationParticipantV2Model(
            operation_id=operation_id,
            actor_id=actor_id,
            role=role,
            expected_response_by=expected_response_by,
        )
        db.add(row)
        db.flush()
        return row

    def list_participants(
        self,
        db: Session,
        *,
        operation_id: str,
        role: str | None = None,
    ) -> list[OperationParticipantV2Model]:
        stmt = select(OperationParticipantV2Model).where(
            OperationParticipantV2Model.operation_id == operation_id,
        )
        if role is not None:
            stmt = stmt.where(OperationParticipantV2Model.role == role)
        return list(db.scalars(stmt))

    def operations_for_actor(
        self,
        db: Session,
        *,
        actor_id: str,
        roles: list[str] | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[tuple[OperationV2Model, str]]:
        """Return (operation, role) pairs for every operation this actor
        participates in. Drives the future Inbox API."""
        stmt = (
            select(OperationV2Model, OperationParticipantV2Model.role)
            .join(
                OperationParticipantV2Model,
                OperationParticipantV2Model.operation_id == OperationV2Model.id,
            )
            .where(OperationParticipantV2Model.actor_id == actor_id)
        )
        if roles:
            stmt = stmt.where(OperationParticipantV2Model.role.in_(roles))
        if state is not None:
            stmt = stmt.where(OperationV2Model.state == state)
        stmt = stmt.order_by(OperationV2Model.updated_at.desc()).limit(max(1, min(int(limit), 1000)))
        return [(op, role) for op, role in db.execute(stmt).all()]

    def update_participant_seen_seq(
        self,
        db: Session,
        *,
        operation_id: str,
        actor_id: str,
        seq: int,
    ) -> None:
        """Per-actor read cursor (analogous to v1 PR21 ChatConversation
        ReadModel but on the unified events log)."""
        rows = list(db.scalars(
            select(OperationParticipantV2Model)
            .where(OperationParticipantV2Model.operation_id == operation_id)
            .where(OperationParticipantV2Model.actor_id == actor_id)
        ))
        for row in rows:
            if seq > (row.last_seen_seq or 0):
                row.last_seen_seq = seq
        db.flush()

    # ---------------- events ----------------

    def insert_event(
        self,
        db: Session,
        *,
        operation_id: str,
        actor_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        addressed_to_actor_ids: list[str] | None = None,
        replies_to_event_id: str | None = None,
        private_to_actor_ids: list[str] | None = None,
        max_retries: int = 3,
    ) -> OperationEventV2Model:
        # Allocate next seq for this operation. SQLite serializes
        # writes in the default isolation, so MAX(seq)+1 is safe under
        # SQLite. Under Postgres / MySQL with concurrent writers two
        # callers can both compute next_seq=N before either INSERTs;
        # the (operation_id, seq) UNIQUE then surfaces an IntegrityError
        # on the loser. G4: catch it, savepoint-rollback, recompute the
        # next seq, retry up to max_retries times.
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        last_error: _IntegrityError | None = None
        for attempt in range(max_retries):
            sp = db.begin_nested()
            try:
                max_seq = db.scalar(
                    select(func.coalesce(func.max(OperationEventV2Model.seq), 0))
                    .where(OperationEventV2Model.operation_id == operation_id)
                ) or 0
                next_seq = int(max_seq) + 1

                row = OperationEventV2Model(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    seq=next_seq,
                    kind=kind,
                    payload_json=_dumps(payload or {}),
                    addressed_to_actor_ids_json=_dumps(addressed_to_actor_ids or []),
                    replies_to_event_id=replies_to_event_id,
                    private_to_actor_ids_json=(
                        _dumps(private_to_actor_ids) if private_to_actor_ids else None
                    ),
                )
                db.add(row)
                db.flush()
                sp.commit()
                return row
            except _IntegrityError as exc:
                sp.rollback()
                last_error = exc
                # If something other than the seq UNIQUE tripped (e.g.
                # FK violation), surface it immediately rather than
                # retrying -- retry won't help.
                msg = str(exc.orig).lower() if exc.orig is not None else ""
                if "seq" not in msg and "unique" not in msg:
                    raise
                if attempt == max_retries - 1:
                    raise
        # Unreachable -- last attempt either returns or raises.
        raise last_error if last_error else RuntimeError("insert_event retry exhausted")

    def count_events(
        self,
        db: Session,
        *,
        operation_id: str,
        kinds: list[str] | None = None,
        kind_prefix: str | None = None,
    ) -> int:
        """Return the number of events recorded for an op, optionally
        filtered. Used by the policy engine to enforce ``max_rounds``
        without paginating the full event log."""
        from sqlalchemy import func as _func, and_ as _and
        clauses = [OperationEventV2Model.operation_id == operation_id]
        if kinds:
            clauses.append(OperationEventV2Model.kind.in_(kinds))
        if kind_prefix:
            clauses.append(OperationEventV2Model.kind.like(f"{kind_prefix}%"))
        stmt = select(_func.count()).select_from(OperationEventV2Model).where(_and(*clauses))
        return int(db.scalar(stmt) or 0)

    def list_events(
        self,
        db: Session,
        *,
        operation_id: str,
        after_seq: int | None = None,
        kinds: list[str] | None = None,
        limit: int = 100,
    ) -> list[OperationEventV2Model]:
        stmt = select(OperationEventV2Model).where(
            OperationEventV2Model.operation_id == operation_id,
        )
        if after_seq is not None:
            stmt = stmt.where(OperationEventV2Model.seq > after_seq)
        if kinds:
            stmt = stmt.where(OperationEventV2Model.kind.in_(kinds))
        stmt = stmt.order_by(OperationEventV2Model.seq.asc()).limit(max(1, min(int(limit), 1000)))
        return list(db.scalars(stmt))

    def event_payload(self, row: OperationEventV2Model) -> dict[str, Any]:
        return dict(_loads(row.payload_json, {}))

    def event_expected_response(self, row: OperationEventV2Model) -> dict[str, Any] | None:
        """Return the v3 expected_response dict for the event, or None
        if the speaker did not declare one. Stored nested in payload at
        ``payload._meta.expected_response`` so the existing payload column
        carries it without a v3-specific schema migration."""
        payload = self.event_payload(row)
        meta = payload.get("_meta") or {}
        if not isinstance(meta, dict):
            return None
        ex = meta.get("expected_response")
        if not isinstance(ex, dict):
            return None
        return ex

    def event_addressed_to(self, row: OperationEventV2Model) -> list[str]:
        return list(_loads(row.addressed_to_actor_ids_json, []))

    def event_private_to(self, row: OperationEventV2Model) -> list[str] | None:
        if row.private_to_actor_ids_json is None:
            return None
        parsed = _loads(row.private_to_actor_ids_json, None)
        return list(parsed) if isinstance(parsed, list) else None

    # ---------------- artifacts ----------------

    def insert_artifact(
        self,
        db: Session,
        *,
        operation_id: str,
        event_id: str,
        kind: str,
        uri: str,
        sha256: str,
        mime: str,
        size_bytes: int,
        label: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OperationArtifactV2Model:
        row = OperationArtifactV2Model(
            operation_id=operation_id,
            event_id=event_id,
            kind=kind,
            uri=uri,
            sha256=sha256,
            mime=mime,
            size_bytes=size_bytes,
            label=label,
            metadata_json=_dumps(metadata or {}),
        )
        db.add(row)
        db.flush()
        return row

    def list_artifacts_for_event(
        self,
        db: Session,
        *,
        event_id: str,
    ) -> list[OperationArtifactV2Model]:
        return list(db.scalars(
            select(OperationArtifactV2Model).where(OperationArtifactV2Model.event_id == event_id)
        ))

    def list_artifacts_for_operation(
        self,
        db: Session,
        *,
        operation_id: str,
        kind: str | None = None,
    ) -> list[OperationArtifactV2Model]:
        stmt = select(OperationArtifactV2Model).where(
            OperationArtifactV2Model.operation_id == operation_id,
        )
        if kind is not None:
            stmt = stmt.where(OperationArtifactV2Model.kind == kind)
        return list(db.scalars(stmt))
