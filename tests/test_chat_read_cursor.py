"""PR21: per-actor read cursor per conversation."""

from __future__ import annotations

import asyncio
import sys

import pytest

from conftest import FakeThreadManager, NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.db as db
    import app.behaviors.chat.service as chat_service_module
    import app.behaviors.chat.conversation_service as conversation_service_module
    import app.behaviors.chat.conversation_schemas as conversation_schemas_module
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.services.remote_task_service as remote_task_service_module

    db.init_db()

    return {
        "schemas": conversation_schemas_module,
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "presence": presence_module, "approvals": approvals_module,
        "remote_task": remote_task_service_module,
    }


def _build(modules):
    tm = FakeThreadManager()
    chat = modules["chat_service_module"].ChatBehaviorService(thread_manager=tm)
    presence = modules["presence"].PresenceService()
    approvals = modules["approvals"].KernelApprovalService()
    remote = modules["remote_task"].RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )
    conv = modules["conversation_service_module"].ChatConversationService(
        remote_task_service=remote,
    )
    return chat, conv


def _open_thread(chat):
    async def go():
        return await chat.create_chat_thread(
            guild_id="g", parent_channel_id="p", title="t",
            topic=None, created_by="alice",
        )
    return asyncio.run(go())


def test_unread_count_starts_at_total_then_zero_after_mark_read(tmp_path, monkeypatch):
    """Initially unread_count = all events; after mark-read it's 0
    (until something new arrives)."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    for i in range(3):
        conv.submit_speech(
            conversation_id=inquiry.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="alice", kind="claim", content=f"msg {i}",
            ),
        )

    # bob has never read -- should see all events as unread
    status = conv.get_conversation_read_status(
        conversation_id=inquiry.id, actor_name="bob",
    )
    # 1 opened + 3 speech = 4 events
    assert status["unread_count"] == 4
    assert status["last_read_speech_id"] is None

    # bob marks all read
    after = conv.mark_conversation_read(
        conversation_id=inquiry.id, actor_name="bob",
    )
    assert after["unread_count"] == 0
    assert after["last_read_speech_id"] is not None


def test_per_actor_cursors_are_independent(tmp_path, monkeypatch):
    """alice and bob track their own cursors independently."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    for i in range(2):
        conv.submit_speech(
            conversation_id=inquiry.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="alice", kind="claim", content=f"msg {i}",
            ),
        )
    # alice catches up (should see 0 unread)
    conv.mark_conversation_read(conversation_id=inquiry.id, actor_name="alice")
    # bob hasn't read; should still see all
    bob_status = conv.get_conversation_read_status(
        conversation_id=inquiry.id, actor_name="bob",
    )
    alice_status = conv.get_conversation_read_status(
        conversation_id=inquiry.id, actor_name="alice",
    )
    assert alice_status["unread_count"] == 0
    assert bob_status["unread_count"] == 3  # 1 opened + 2 speech


def test_unread_count_increments_after_new_speech(tmp_path, monkeypatch):
    """After mark-read, new speech raises the count for that actor."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="m1",
        ),
    )
    conv.mark_conversation_read(conversation_id=inquiry.id, actor_name="bob")
    after_first = conv.get_conversation_read_status(
        conversation_id=inquiry.id, actor_name="bob",
    )
    assert after_first["unread_count"] == 0

    # New speech arrives
    conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="m2",
        ),
    )
    after_second = conv.get_conversation_read_status(
        conversation_id=inquiry.id, actor_name="bob",
    )
    assert after_second["unread_count"] == 1


def test_mark_read_to_specific_speech_id(tmp_path, monkeypatch):
    """Marking to a specific speech (not latest) -- newer speech
    remain unread."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    s1 = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="1",
        ),
    )
    s2 = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="2",
        ),
    )
    s3 = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="3",
        ),
    )
    # bob marks read up to s1
    conv.mark_conversation_read(
        conversation_id=inquiry.id, actor_name="bob", speech_id=s1.id,
    )
    status = conv.get_conversation_read_status(
        conversation_id=inquiry.id, actor_name="bob",
    )
    # 2 speech rows newer than s1 (s2, s3) remain unread
    assert status["unread_count"] == 2
