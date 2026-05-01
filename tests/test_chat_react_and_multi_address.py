"""PR20: 'react' speech kind + multi-address support."""

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


def test_react_speech_kind_persists(tmp_path, monkeypatch):
    """'react' is now a first-class SpeechKind for low-noise
    acknowledgement (👍, ack, noted). Persists like other kinds."""
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
    react = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob", kind="react", content=":+1:",
        ),
    )
    assert react.kind == "react"
    detail = conv.get_conversation(conversation_id=inquiry.id)
    react_rows = [s for s in detail.recent_speech if s.kind == "react"]
    assert len(react_rows) == 1


def test_multi_address_primary_plus_extras(tmp_path, monkeypatch):
    """Passing addressed_to + addressed_to_many records both. The
    primary drives expected_speaker for turn-taking; extras are
    visible in the summary."""
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
    speech = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="question",
            content="@bob @carol can either of you confirm?",
            addressed_to="bob",
            addressed_to_many=["bob", "carol"],
        ),
    )
    assert speech.addressed_to == "bob"
    assert speech.addressed_to_many == ["bob", "carol"]

    # The conversation's expected_speaker tracks the primary
    detail = conv.get_conversation(conversation_id=inquiry.id)
    assert detail.conversation.expected_speaker == "bob"


def test_multi_address_only_extras_lifts_first_to_primary(tmp_path, monkeypatch):
    """If only addressed_to_many is provided, the first element is
    lifted into the primary slot so turn-taking still works without
    forcing the caller to duplicate."""
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
    speech = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="question", content="?",
            addressed_to_many=["bob", "carol", "dave"],
        ),
    )
    assert speech.addressed_to == "bob"
    assert speech.addressed_to_many == ["bob", "carol", "dave"]
    detail = conv.get_conversation(conversation_id=inquiry.id)
    assert detail.conversation.expected_speaker == "bob"


def test_multi_address_dedupes_and_strips(tmp_path, monkeypatch):
    """Whitespace + dupes are normalized at request validation."""
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
    speech = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="question", content="?",
            addressed_to="bob",
            addressed_to_many=["bob", " bob ", "carol", "", "carol"],
        ),
    )
    # bob is primary; carol is the only extra (dupes/empty stripped)
    assert speech.addressed_to == "bob"
    assert speech.addressed_to_many == ["bob", "carol"]
