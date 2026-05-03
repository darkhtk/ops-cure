"""PR15: speech reply chain via replies_to_speech_id."""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

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
    import app.behaviors.chat.models as chat_models
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.services.remote_task_service as remote_task_service_module

    db.init_db()

    return {
        "db": db, "schemas": conversation_schemas_module,
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "chat_models": chat_models, "presence": presence_module,
        "approvals": approvals_module, "remote_task": remote_task_service_module,
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


def test_reply_chain_links_speech_to_parent(tmp_path, monkeypatch):
    """Speech with replies_to_speech_id persists the link and exposes
    it on SpeechActSummary so clients can render nested threads."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    db = modules["db"]
    chat, conv = _build(modules)

    thread = _open_thread(chat)
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    q = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="question",
            content="who's reviewing PR #302?",
        ),
    )
    a = conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob", kind="answer", content="me; on it",
            replies_to_speech_id=q.id,
        ),
    )
    assert a.replies_to_speech_id == q.id
    assert q.replies_to_speech_id is None

    detail = conv.get_conversation(conversation_id=inquiry.id)
    by_id = {row.id: row for row in detail.recent_speech}
    assert by_id[a.id].replies_to_speech_id == q.id


def test_reply_chain_n_deep(tmp_path, monkeypatch):
    """A chain of replies (a -> b -> c -> d) all link cleanly."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)
    proposal = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal", title="design", opener_actor="alice",
        ),
    )
    a = conv.submit_speech(
        conversation_id=proposal.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="propose", content="approach A",
        ),
    )
    b = conv.submit_speech(
        conversation_id=proposal.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob", kind="object", content="A breaks X",
            replies_to_speech_id=a.id,
        ),
    )
    c = conv.submit_speech(
        conversation_id=proposal.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="agree", content="fair",
            replies_to_speech_id=b.id,
        ),
    )
    d = conv.submit_speech(
        conversation_id=proposal.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="propose", content="approach A'",
            replies_to_speech_id=c.id,
        ),
    )
    assert (a.replies_to_speech_id, b.replies_to_speech_id,
            c.replies_to_speech_id, d.replies_to_speech_id) == (None, a.id, b.id, c.id)


def test_reply_to_nonexistent_id_is_rejected_by_fk(tmp_path, monkeypatch):
    """Dangling reply references are rejected at write time. v3 phase
    2.5 turned ``PRAGMA foreign_keys=ON`` (alongside WAL) which
    promoted this from "silently persisted" to "IntegrityError".
    The new behavior is the correct one — a reply pointer is a hard
    reference, not informational."""
    import pytest
    from sqlalchemy.exc import IntegrityError
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
    with pytest.raises(IntegrityError):
        conv.submit_speech(
            conversation_id=inquiry.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="alice", kind="claim", content="ref'ing a stale id",
                replies_to_speech_id="nonexistent-message-id",
            ),
        )
