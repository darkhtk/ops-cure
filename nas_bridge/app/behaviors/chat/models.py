"""Chat behavior persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


CONVERSATION_KIND_GENERAL = "general"
CONVERSATION_KIND_INQUIRY = "inquiry"
CONVERSATION_KIND_PROPOSAL = "proposal"
CONVERSATION_KIND_TASK = "task"

CONVERSATION_STATE_OPEN = "open"
CONVERSATION_STATE_RESOLVING = "resolving"
CONVERSATION_STATE_CLOSED = "closed"


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


class ChatParticipantStateModel(Base):
    __tablename__ = "chat_participant_states"
    __table_args__ = (
        UniqueConstraint("thread_id", "actor_name", name="uq_chat_participant_state_per_thread"),
        Index("ix_chat_participant_states_thread_seen", "thread_id", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True)
    actor_name: Mapped[str] = mapped_column(index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_read_message_id: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True)
    last_read_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatConversationModel(Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index("ix_chat_conversations_thread_state", "thread_id", "state"),
        Index("ix_chat_conversations_thread_kind", "thread_id", "kind"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(index=True, default=CONVERSATION_KIND_GENERAL)
    title: Mapped[str] = mapped_column(Text())
    intent: Mapped[str | None] = mapped_column(Text(), nullable=True)
    state: Mapped[str] = mapped_column(index=True, default=CONVERSATION_STATE_OPEN)
    opener_actor: Mapped[str] = mapped_column(index=True)
    owner_actor: Mapped[str | None] = mapped_column(index=True, nullable=True)
    expected_speaker: Mapped[str | None] = mapped_column(index=True, nullable=True)
    parent_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    bound_task_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
    resolution: Mapped[str | None] = mapped_column(index=True, nullable=True)
    resolution_summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(index=True, nullable=True)
    is_general: Mapped[bool] = mapped_column(Boolean(), default=False, index=True)
    last_speech_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    speech_count: Mapped[int] = mapped_column(Integer(), default=0)
    idle_warning_emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Multi-tier idle escalation: 0 = no warnings yet, 1 = tier-1 emitted,
    # 2 = tier-2 emitted, 3 = tier-3 reached (conversation auto-abandoned).
    idle_warning_count: Mapped[int] = mapped_column(Integer(), default=0)
    # Soft turn-taking gauge: number of speech acts since the last
    # expected_speaker change that came from someone OTHER than the
    # expected_speaker. Visible to clients/operators but not enforced
    # by the system in v1.
    unaddressed_speech_count: Mapped[int] = mapped_column(Integer(), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatMetricSnapshotModel(Base):
    """Persistent snapshot of ChatRoomMetrics (PR17). The in-memory
    metrics on ``ChatConversationService.metrics`` reset on bridge
    restart -- this table captures point-in-time snapshots so
    operators can see trends over hours/days. Either thread-scoped
    or global (thread_id is NULL); call sites pick which.

    The full counter state is stored as a JSON blob so adding new
    metric fields later doesn't require a schema change.
    """

    __tablename__ = "chat_metric_snapshots"
    __table_args__ = (
        Index("ix_chat_metric_snap_thread_at", "thread_id", "captured_at"),
        Index("ix_chat_metric_snap_global_at", "captured_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        nullable=True,
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    snapshot_json: Mapped[str] = mapped_column(Text(), default="{}")


class ChatConversationReadModel(Base):
    """Per-actor read cursor for a single conversation. PR21.

    ChatParticipantStateModel already tracks read state at the THREAD
    level (legacy from before PR1 conversations existed); this table
    is the conversation-level equivalent so a participant can have
    different unread counts per conversation in the same thread.
    """

    __tablename__ = "chat_conversation_reads"
    __table_args__ = (
        UniqueConstraint("conversation_id", "actor_name", name="uq_chat_conv_read_per_actor"),
        Index("ix_chat_conv_reads_actor", "actor_name"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"),
        index=True,
    )
    actor_name: Mapped[str] = mapped_column(index=True)
    last_read_speech_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class ChatMessageModel(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_thread_created", "thread_id", "created_at"),
        Index("ix_chat_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_name: Mapped[str] = mapped_column(index=True)
    event_kind: Mapped[str] = mapped_column(Text(), default="claim")
    addressed_to: Mapped[str | None] = mapped_column(Text(), nullable=True, index=True)
    # PR20 multi-address: optional JSON-encoded list of additional
    # addressees beyond the primary ``addressed_to``. ``addressed_to``
    # remains the canonical single slot (drives expected_speaker);
    # this field carries the full set so renderers can show "@alice
    # @bob @carol" when needed without bloating the indexed column.
    addressed_to_many_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # PR15 reply chain: optional pointer to a prior speech act this
    # message replies to. Lets clients render nested threads instead
    # of flat lists once a conversation grows past ~6 turns.
    replies_to_speech_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
