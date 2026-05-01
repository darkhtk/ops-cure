"""Conversation protocol lifecycle tests for the chat behavior.

These tests cover the PR1 surface:

- thread creation bootstraps a ``general`` conversation
- chat messages submitted via the legacy API stamp the message with the
  thread's general conversation id and bump its speech counters
- ``open_conversation`` / ``submit_speech`` / ``close_conversation``
  for a typed conversation (``inquiry``)
- ``general`` conversation cannot be closed
- speech against a closed conversation is rejected
- ``addressed_to`` updates ``expected_speaker``
- ``backfill_general_conversations`` assigns orphan messages to general
"""

from __future__ import annotations

import asyncio
import sys

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

    db.init_db()

    return {
        "db": db,
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "conversation_schemas_module": conversation_schemas_module,
        "chat_models": chat_models,
    }


def _build_services(modules):
    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(
        thread_manager=thread_manager,
    )
    conversation_service = modules["conversation_service_module"].ChatConversationService()
    return thread_manager, chat_service, conversation_service


def test_create_thread_bootstraps_general_conversation(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    chat_models = modules["chat_models"]
    db = modules["db"]
    _, chat_service, _ = _build_services(modules)

    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic="ai pair programming",
            created_by="alice",
        )

    created = asyncio.run(scenario())

    with db.session_scope() as session:
        rows = list(
            session.scalars(
                select(chat_models.ChatConversationModel).where(
                    chat_models.ChatConversationModel.thread_id == created.id,
                ),
            ),
        )
        assert len(rows) == 1
        general = rows[0]
        assert general.is_general is True
        assert general.kind == chat_models.CONVERSATION_KIND_GENERAL
        assert general.state == chat_models.CONVERSATION_STATE_OPEN
        assert general.opener_actor == "system"
        assert general.title == "General"


def test_submit_message_stamps_general_conversation_id(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    chat_models = modules["chat_models"]
    db = modules["db"]
    _, chat_service, _ = _build_services(modules)

    async def scenario():
        thread = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic=None,
            created_by="alice",
        )
        return thread

    thread = asyncio.run(scenario())

    response = chat_service.submit_participant_message(
        thread_id=thread.discord_thread_id,
        actor_name="alice",
        actor_kind="human",
        content="hello room",
    )
    assert response is not None

    with db.session_scope() as session:
        general = session.scalar(
            select(chat_models.ChatConversationModel).where(
                chat_models.ChatConversationModel.thread_id == thread.id,
            ),
        )
        assert general is not None
        assert general.speech_count == 1
        assert general.last_speech_at is not None
        general_id = general.id

        message = session.scalar(
            select(chat_models.ChatMessageModel).where(
                chat_models.ChatMessageModel.thread_id == thread.id,
            ),
        )
        assert message is not None
        assert message.conversation_id == general_id
        assert message.event_kind == "claim"


def test_open_close_inquiry_conversation_lifecycle(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    chat_models = modules["chat_models"]
    db = modules["db"]
    schemas = modules["conversation_schemas_module"]
    _, chat_service, conversation_service = _build_services(modules)

    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic=None,
            created_by="alice",
        )

    thread = asyncio.run(scenario())

    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="What is the auth token rotation policy?",
            opener_actor="alice",
            intent="Need answer before next deploy.",
            addressed_to="bob",
        ),
    )
    assert opened.kind == "inquiry"
    assert opened.state == "open"
    assert opened.opener_actor == "alice"
    assert opened.expected_speaker == "bob"
    assert opened.is_general is False

    speech = conversation_service.submit_speech(
        conversation_id=opened.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob",
            kind="answer",
            content="It rotates every 90 days, last rotation 2026-04-15.",
        ),
    )
    assert speech.kind == "answer"
    assert speech.actor_name == "bob"

    closed = conversation_service.close_conversation(
        conversation_id=opened.id,
        closed_by="alice",
        resolution="answered",
        summary="Got the rotation date.",
    )
    assert closed.state == "closed"
    assert closed.resolution == "answered"
    assert closed.closed_by == "alice"
    assert closed.closed_at is not None

    with db.session_scope() as session:
        all_events = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == opened.id)
                .order_by(chat_models.ChatMessageModel.created_at.asc()),
            ),
        )
        event_kinds = [event.event_kind for event in all_events]
        assert event_kinds == [
            "chat.conversation.opened",
            "chat.speech.answer",
            "chat.conversation.closed",
        ]


def test_close_general_is_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    chat_models = modules["chat_models"]
    db = modules["db"]
    conv_module = modules["conversation_service_module"]
    _, chat_service, conversation_service = _build_services(modules)

    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic=None,
            created_by="alice",
        )

    thread = asyncio.run(scenario())

    with db.session_scope() as session:
        general = session.scalar(
            select(chat_models.ChatConversationModel).where(
                chat_models.ChatConversationModel.thread_id == thread.id,
            ),
        )
        general_id = general.id

    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.close_conversation(
            conversation_id=general_id,
            closed_by="alice",
            resolution="dropped",
            summary="trying to close general",
        )


def test_speech_on_closed_conversation_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]
    _, chat_service, conversation_service = _build_services(modules)

    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic=None,
            created_by="alice",
        )

    thread = asyncio.run(scenario())

    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal",
            title="Adopt evidence-required heartbeat",
            opener_actor="alice",
        ),
    )
    conversation_service.close_conversation(
        conversation_id=opened.id,
        closed_by="alice",
        resolution="accepted",
    )

    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.submit_speech(
            conversation_id=opened.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="bob",
                kind="claim",
                content="late comment",
            ),
        )


def test_addressed_to_updates_expected_speaker(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    _, chat_service, conversation_service = _build_services(modules)

    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic=None,
            created_by="alice",
        )

    thread = asyncio.run(scenario())

    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="Who reviews the migration?",
            opener_actor="alice",
        ),
    )
    assert opened.expected_speaker is None

    conversation_service.submit_speech(
        conversation_id=opened.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice",
            kind="question",
            content="who?",
            addressed_to="bob",
        ),
    )
    detail = conversation_service.get_conversation(conversation_id=opened.id)
    assert detail.conversation.expected_speaker == "bob"

    conversation_service.submit_speech(
        conversation_id=opened.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob",
            kind="answer",
            content="me",
        ),
    )
    detail = conversation_service.get_conversation(conversation_id=opened.id)
    assert detail.conversation.expected_speaker is None


def test_backfill_attaches_orphan_messages_to_general(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    chat_models = modules["chat_models"]
    db = modules["db"]
    _, chat_service, conversation_service = _build_services(modules)

    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic=None,
            created_by="alice",
        )

    thread = asyncio.run(scenario())

    # Simulate a legacy row that pre-dates the conversation column by
    # inserting a message with NULL conversation_id and the old
    # event_kind string.
    with db.session_scope() as session:
        legacy = chat_models.ChatMessageModel(
            thread_id=thread.id,
            conversation_id=None,
            actor_name="alice",
            event_kind="message",
            content="legacy message before backfill",
        )
        session.add(legacy)
        legacy_id = None
        session.flush()
        legacy_id = legacy.id

    migrated = conversation_service.backfill_general_conversations()
    assert migrated == 1

    with db.session_scope() as session:
        legacy_row = session.get(chat_models.ChatMessageModel, legacy_id)
        general = session.scalar(
            select(chat_models.ChatConversationModel).where(
                chat_models.ChatConversationModel.thread_id == thread.id,
            ),
        )
        assert legacy_row is not None
        assert general is not None
        assert legacy_row.conversation_id == general.id
        assert legacy_row.event_kind == "claim"
