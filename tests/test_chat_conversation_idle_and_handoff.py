"""PR3: idle sweep + owner handoff coverage.

- ``sweep_idle_conversations`` flags only open, non-general conversations
  whose last activity is older than the threshold AND that have not
  already been warned.
- the warning is idempotent: a second sweep at the same age does not
  re-emit.
- ``transfer_owner`` updates ``owner_actor`` + ``expected_speaker`` and
  emits a ``chat.conversation.handoff`` event.
- ``transfer_owner`` is rejected for general or for task-bound
  conversations (lease should govern those).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from conftest import FakeThreadManager, NAS_BRIDGE_ROOT


def _bootstrap_app(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.db as db
    import app.behaviors.chat.service as chat_service_module
    import app.behaviors.chat.conversation_service as conversation_service_module
    import app.behaviors.chat.conversation_schemas as conversation_schemas_module
    import app.behaviors.chat.models as chat_models
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.services.remote_task_service as remote_task_service_module

    db.init_db()

    return {
        "db": db,
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "conversation_schemas_module": conversation_schemas_module,
        "chat_models": chat_models,
        "presence_module": presence_module,
        "approvals_module": approvals_module,
        "remote_task_service_module": remote_task_service_module,
    }


def _build(modules):
    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(
        thread_manager=thread_manager,
    )
    presence = modules["presence_module"].PresenceService()
    approvals = modules["approvals_module"].KernelApprovalService()
    remote_task_service = modules["remote_task_service_module"].RemoteTaskService(
        presence_service=presence,
        kernel_approval_service=approvals,
    )
    conversation_service = modules["conversation_service_module"].ChatConversationService(
        remote_task_service=remote_task_service,
    )
    return chat_service, conversation_service, remote_task_service


def _open_thread(chat_service, *, title="collab room") -> object:
    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title=title,
            topic=None,
            created_by="alice",
        )

    return asyncio.run(scenario())


def test_sweep_flags_idle_open_conversation_once(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)
    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="What's the rotation policy?",
            opener_actor="alice",
            addressed_to="bob",
        ),
    )

    # Backdate created_at + last_speech_at to simulate 60min of silence.
    backdated = datetime.now(timezone.utc) - timedelta(minutes=60)
    with db.session_scope() as session:
        row = session.get(chat_models.ChatConversationModel, opened.id)
        row.created_at = backdated
        row.last_speech_at = backdated

    flagged = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    assert len(flagged) == 1
    assert flagged[0].id == opened.id
    assert flagged[0].idle_warning_emitted_at is not None

    # Idempotent: a second sweep at the same threshold does not re-emit.
    flagged_again = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    assert flagged_again == []

    with db.session_scope() as session:
        events = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == opened.id)
                .where(chat_models.ChatMessageModel.event_kind == "chat.conversation.idle_warning")
            ),
        )
        assert len(events) == 1


def test_sweep_skips_recently_active_general_and_closed(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)

    # 1) general — must never be flagged
    # 2) recent open inquiry — under threshold, must not be flagged
    recent = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="recent inquiry",
            opener_actor="alice",
        ),
    )
    # 3) closed proposal — closed should be skipped even if old
    closed = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal",
            title="something",
            opener_actor="alice",
        ),
    )
    conversation_service.close_conversation(
        conversation_id=closed.id,
        closed_by="alice",
        resolution="withdrawn",
    )
    backdated = datetime.now(timezone.utc) - timedelta(minutes=60)
    with db.session_scope() as session:
        # Backdate general too — it must still be skipped.
        general = session.scalar(
            select(chat_models.ChatConversationModel)
            .where(chat_models.ChatConversationModel.thread_id == thread.id)
            .where(chat_models.ChatConversationModel.is_general.is_(True))
        )
        general.created_at = backdated
        general.last_speech_at = backdated
        # Backdate closed too.
        closed_row = session.get(chat_models.ChatConversationModel, closed.id)
        closed_row.created_at = backdated
        closed_row.last_speech_at = backdated

    flagged = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    assert flagged == []
    # The recent inquiry must still be open, untouched.
    detail = conversation_service.get_conversation(conversation_id=recent.id)
    assert detail.conversation.idle_warning_emitted_at is None


def test_transfer_owner_updates_owner_and_expected_speaker(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)
    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal",
            title="adopt evidence-required heartbeats",
            opener_actor="alice",
            owner_actor="alice",
        ),
    )

    transferred = conversation_service.transfer_owner(
        conversation_id=opened.id,
        by_actor="alice",
        new_owner="bob",
        reason="bob has more context",
    )
    assert transferred.owner_actor == "bob"
    assert transferred.expected_speaker == "bob"

    with db.session_scope() as session:
        events = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == opened.id)
                .where(chat_models.ChatMessageModel.event_kind == "chat.conversation.handoff")
            ),
        )
        assert len(events) == 1
        assert events[0].addressed_to == "bob"


def test_transfer_owner_rejected_for_task_bound_and_general(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)

    # Task-bound conversation: handoff must be rejected.
    task_conv = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="bound task",
            opener_actor="alice",
            objective="do work",
        ),
    )
    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.transfer_owner(
            conversation_id=task_conv.id,
            by_actor="alice",
            new_owner="bob",
        )

    # General conversation: also rejected.
    with db.session_scope() as session:
        general = session.scalar(
            select(chat_models.ChatConversationModel)
            .where(chat_models.ChatConversationModel.thread_id == thread.id)
            .where(chat_models.ChatConversationModel.is_general.is_(True))
        )
        general_id = general.id
    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.transfer_owner(
            conversation_id=general_id,
            by_actor="alice",
            new_owner="bob",
        )
