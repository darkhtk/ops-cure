"""PR16: bulk close + audit log query."""

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
    import app.behaviors.chat.task_coordinator as task_coordinator_module
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.services.remote_task_service as remote_task_service_module

    db.init_db()

    return {
        "schemas": conversation_schemas_module,
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "task_coordinator_module": task_coordinator_module,
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
    coord = modules["task_coordinator_module"].ChatTaskCoordinator(
        conversation_service=conv, remote_task_service=remote,
    )
    return chat, conv, coord


def _open_thread(chat, title="t"):
    async def go():
        return await chat.create_chat_thread(
            guild_id="g", parent_channel_id="p", title=title,
            topic=None, created_by="alice",
        )
    return asyncio.run(go())


def test_bulk_close_partial_success(tmp_path, monkeypatch):
    """Bulk close: 3 inquiries (2 closeable, 1 task-bound).
    Per-id results show 2 ok and 1 error without aborting."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv, coord = _build(modules)
    thread = _open_thread(chat)

    a = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="a", opener_actor="alice",
        ),
    )
    b = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="b", opener_actor="alice",
        ),
    )
    # task-bound conversation -- bulk close should fail this one
    c = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task", title="c", opener_actor="alice", objective="x",
        ),
    )
    coord.claim(
        conversation_id=c.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )

    snap = conv.bulk_close_conversations(
        conversation_ids=[a.id, b.id, c.id],
        closed_by="alice", resolution="dropped",
    )
    assert snap["requested"] == 3
    assert snap["succeeded"] == 2
    assert snap["failed"] == 1

    by_id = {r["conversation_id"]: r for r in snap["results"]}
    assert by_id[a.id]["ok"] is True
    assert by_id[b.id]["ok"] is True
    assert by_id[c.id]["ok"] is False
    # Task conversation rejects close with inquiry-vocabulary
    # resolution 'dropped'; the task-bound guard would also fire
    # but the resolution-vocab check trips first. Either way the
    # bulk operation captures it as a per-id error.
    assert "ChatConversationStateError" in by_id[c.id]["error"]


def test_audit_log_filters_combine(tmp_path, monkeypatch):
    """Search by actor + event_kind_prefix returns only matching
    rows; total count matches what was emitted."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv, _ = _build(modules)
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
            actor_name="alice", kind="question", content="x?",
        ),
    )
    conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob", kind="answer", content="y",
        ),
    )
    conv.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="ack",
        ),
    )

    # all alice events on this thread
    snap = conv.search_audit_log(
        thread_id=thread.discord_thread_id, actor_name="alice",
    )
    kinds = [item["event_kind"] for item in snap["items"]]
    # alice opened the inquiry + 2 speech rows = 3 events
    assert len(snap["items"]) == 3
    # newest first
    assert snap["items"][0]["created_at"] >= snap["items"][-1]["created_at"]

    # only speech events
    snap2 = conv.search_audit_log(
        thread_id=thread.discord_thread_id, event_kind_prefix="chat.speech.",
    )
    assert len(snap2["items"]) == 3
    assert all(it["event_kind"].startswith("chat.speech.") for it in snap2["items"])

    # only one specific kind
    snap3 = conv.search_audit_log(
        thread_id=thread.discord_thread_id, event_kind="chat.speech.question",
    )
    assert len(snap3["items"]) == 1
    assert snap3["items"][0]["actor_name"] == "alice"


def test_audit_log_pagination(tmp_path, monkeypatch):
    """Limit + offset pagination with has_more flag."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv, _ = _build(modules)
    thread = _open_thread(chat)
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    for i in range(7):
        conv.submit_speech(
            conversation_id=inquiry.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="alice", kind="claim", content=f"msg {i}",
            ),
        )

    page1 = conv.search_audit_log(
        thread_id=thread.discord_thread_id, limit=3, offset=0,
    )
    assert len(page1["items"]) == 3
    assert page1["has_more"] is True
    assert page1["next_cursor"] == "3"

    page2 = conv.search_audit_log(
        thread_id=thread.discord_thread_id, limit=3, offset=3,
    )
    assert len(page2["items"]) == 3
    assert page2["has_more"] is True

    page3 = conv.search_audit_log(
        thread_id=thread.discord_thread_id, limit=3, offset=6,
    )
    # remaining: 1 inquiry-opened + 7 speech = 8 total; page3 with offset=6 has 2 more
    assert len(page3["items"]) == 2
    assert page3["has_more"] is False
