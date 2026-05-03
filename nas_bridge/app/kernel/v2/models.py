"""SQLAlchemy models for protocol v2.

The five tables map directly onto the v2 design:

- ``ActorModel``                   identity is now an entity, not a string
- ``OperationModel``               Conversation+Task unified
- ``OperationParticipantModel``    many-to-many actor<->op with role
- ``OperationEventModel``          speech + lifecycle on one log
- ``OperationArtifactModel``       multi-modal evidence references

Design rationale lives in the parent message of the F1 commit; this
module is intentionally just the schema with light comments. No
business rules, no validators -- those go in the Repository / future
service layer.

Conventions:
- All ids are uuid4 strings (consistent with v1 ChatConversationModel etc.).
- All timestamps are timezone-aware UTC.
- JSON-shaped fields land as ``Text`` columns + caller-side ``json.dumps`` /
  ``json.loads``. SQLite ``JSON`` type is supported but tests/portability
  prefer plain text.
- Cascade-on-delete is wired so dropping an Operation cleans events,
  participants, and artifacts in one shot.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# ActorModel
# -----------------------------------------------------------------------------


class ActorTokenV2Model(Base):
    """v3 phase 3.x — per-actor token issuance.

    Binds a long-lived bearer token to a single actor row. The token's
    sha256 hash is stored; the plaintext is returned exactly once at
    issue time and never persisted. A ``revoked_at`` timestamp soft-
    revokes a token without deleting the audit row.

    The issue/revoke flow itself is gated by the shared admin bearer
    (existing ``BRIDGE_SHARED_AUTH_TOKEN``) so a compromised actor
    token cannot mint new tokens for itself.
    """

    __tablename__ = "actor_tokens_v2"
    __table_args__ = (
        Index("ix_actor_tokens_v2_token_hash", "token_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actors_v2.id", ondelete="CASCADE"),
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(Text())
    label: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActorV2Model(Base):
    """First-class identity. Every speaker / claimer / approver / observer
    in v2 is a row here, looked up by ``handle`` (e.g. ``@alice``,
    ``@claude-pca``, ``@system``).

    ``capabilities`` is a JSON-encoded list of permission strings such as
    ``["close_inquiry", "approve_destructive", "claim_task"]``. A
    capability authorizer in F9 will check these against operation
    transitions.

    ``public_key`` is reserved for a future signed-payload identity
    scheme (e.g. each AI agent generates a keypair, the bridge stores
    the pubkey, every speech act includes a signature). Until that
    lands, the column stays NULL and identity is enforced via the
    ``actor_authorizer`` callback added in v1 PR13.
    """

    __tablename__ = "actors_v2"
    __table_args__ = (
        Index("ix_actors_v2_status_kind", "status", "kind"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    handle: Mapped[str] = mapped_column(unique=True, index=True)
    display_name: Mapped[str] = mapped_column(Text())
    kind: Mapped[str] = mapped_column(index=True, default="ai")  # human | ai | service | system
    capabilities_json: Mapped[str] = mapped_column(Text(), default="[]")
    public_key: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(index=True, default="offline")  # online | idle | offline
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


# -----------------------------------------------------------------------------
# OperationModel  --  unified Conversation + Task
# -----------------------------------------------------------------------------


class OperationV2Model(Base):
    """The single primitive replacing v1 ChatConversationModel +
    RemoteTaskModel.

    ``space_id`` is a free-form scope identifier; the convention is
    ``"<behavior>:<scope-id>"`` e.g. ``"chat:thread-uuid-abc"``. Replaces
    v1's ``(machine_id, thread_id)`` pair (the long-standing PR18 debt).

    ``kind`` drives the state machine and which fields in
    ``metadata_json`` are required:
    - ``inquiry``   -> {expected_speaker?}
    - ``proposal``  -> {voters[], voting_close_at?}
    - ``task``      -> {objective, success_criteria, lease holder, evidence required}
    - ``incident``  -> {severity, sla_minutes}
    - ``decision``  -> {options[], required_approvals[]}
    - ``general``   -> {} (the always-open thread-level chat catchall)

    State transition rules per kind ship in F10. F1 just stores the
    string and trusts callers.

    ``deadline_at`` + ``on_deadline_action`` give us auto-handoff /
    auto-abandon semantics without an external scheduler -- the idle
    sweep already runs periodically and can act on this.
    """

    __tablename__ = "operations_v2"
    __table_args__ = (
        Index("ix_operations_v2_space_state", "space_id", "state"),
        Index("ix_operations_v2_space_kind_state", "space_id", "kind", "state"),
        Index("ix_operations_v2_deadline", "deadline_at", "state"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    space_id: Mapped[str] = mapped_column(index=True)
    parent_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations_v2.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind: Mapped[str] = mapped_column(index=True)
    state: Mapped[str] = mapped_column(index=True, default="open")
    title: Mapped[str] = mapped_column(Text())
    intent: Mapped[str | None] = mapped_column(Text(), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    on_deadline_action: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text(), default="{}")
    resolution: Mapped[str | None] = mapped_column(index=True, nullable=True)
    resolution_summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    closed_by_actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("actors_v2.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relations cascade on operation delete -- events / participants /
    # artifacts disappear together. parent_operation_id is intentionally
    # SET NULL on parent delete (don't cascade-delete children of a
    # deleted parent; they may matter standalone).
    participants: Mapped[list["OperationParticipantV2Model"]] = relationship(
        back_populates="operation",
        cascade="all, delete-orphan",
    )
    events: Mapped[list["OperationEventV2Model"]] = relationship(
        back_populates="operation",
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list["OperationArtifactV2Model"]] = relationship(
        back_populates="operation",
        cascade="all, delete-orphan",
    )


# -----------------------------------------------------------------------------
# OperationParticipantModel
# -----------------------------------------------------------------------------


class OperationParticipantV2Model(Base):
    """Who is involved in this operation, in what role.

    A single (operation, actor) pair can hold MULTIPLE roles -- the
    opener of a task may also be the addressed reviewer of the bound
    decision. Hence the composite UNIQUE on (operation, actor, role)
    rather than (operation, actor).

    Roles (open set, callers pick):
      opener      -- created the operation
      owner       -- currently holds the lease / responsible for next step
      addressed   -- expected to respond (drives turn-taking visibility)
      observer    -- subscribes for awareness only
      approver    -- gate-keeps a kind=decision or kind=task approval
      reviewer    -- expected to weigh in on a kind=proposal
    """

    __tablename__ = "operation_participants_v2"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "actor_id", "role",
            name="uq_op_participants_v2_op_actor_role",
        ),
        Index("ix_op_participants_v2_actor_role", "actor_id", "role"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations_v2.id", ondelete="CASCADE"),
        index=True,
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actors_v2.id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[str] = mapped_column(index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expected_response_by: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_seen_seq: Mapped[int] = mapped_column(Integer(), default=0)

    operation: Mapped[OperationV2Model] = relationship(back_populates="participants")


# -----------------------------------------------------------------------------
# OperationEventModel  --  speech + lifecycle on one log
# -----------------------------------------------------------------------------


class OperationEventV2Model(Base):
    """Every state change is an event row. Speech, claim, evidence,
    lifecycle transition, approval request/resolve, handoff -- all the
    same shape with a discriminating ``kind`` and a structured
    ``payload_json``. The Operation's current state is a projection
    derived from the latest events (event-sourced).

    ``seq`` is monotonically increasing per Operation. The Repository
    enforces this with a ``MAX(seq)+1`` insert under SQLite's default
    serializable isolation.

    ``replies_to_event_id`` is universal -- not just for speech.
    Approvals can reply to evidence; evidence can reply to a question;
    etc.

    ``private_to_actor_ids`` is null for public events. When set,
    only the listed actors should see this event -- enforced by the
    subscription-side filter (added in F5).
    """

    __tablename__ = "operation_events_v2"
    __table_args__ = (
        UniqueConstraint("operation_id", "seq", name="uq_op_events_v2_op_seq"),
        Index("ix_op_events_v2_actor_created", "actor_id", "created_at"),
        Index("ix_op_events_v2_kind_created", "kind", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations_v2.id", ondelete="CASCADE"),
        index=True,
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actors_v2.id", ondelete="CASCADE"),
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer())
    kind: Mapped[str] = mapped_column(index=True)
    payload_json: Mapped[str] = mapped_column(Text(), default="{}")
    addressed_to_actor_ids_json: Mapped[str] = mapped_column(Text(), default="[]")
    replies_to_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("operation_events_v2.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    private_to_actor_ids_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    operation: Mapped[OperationV2Model] = relationship(back_populates="events")


# -----------------------------------------------------------------------------
# OperationArtifactModel  --  multi-modal evidence references
# -----------------------------------------------------------------------------


class OperationArtifactV2Model(Base):
    """Reference to a file / image / diff / log living in external
    storage (NAS volume, S3, local filesystem). The bridge does not
    store the bytes -- it stores a URI + integrity metadata so any
    consumer can fetch and verify.

    ``event_id`` ties the artifact to the event that introduced it
    (typically a kind=evidence event) so audit queries can navigate
    'show me everything codex-pcb posted on this incident'.
    """

    __tablename__ = "operation_artifacts_v2"
    __table_args__ = (
        Index("ix_op_artifacts_v2_event", "event_id"),
        Index("ix_op_artifacts_v2_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations_v2.id", ondelete="CASCADE"),
        index=True,
    )
    event_id: Mapped[str] = mapped_column(
        ForeignKey("operation_events_v2.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(index=True)  # screenshot | diff | log | file | image | audio
    uri: Mapped[str] = mapped_column(Text())
    sha256: Mapped[str] = mapped_column(Text())
    mime: Mapped[str] = mapped_column(Text())
    size_bytes: Mapped[int] = mapped_column(Integer())
    label: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text(), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    operation: Mapped[OperationV2Model] = relationship(back_populates="artifacts")
