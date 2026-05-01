"""PR17: persistent metric snapshots + latency stats."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

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


def test_capture_and_retrieve_global_snapshot(tmp_path, monkeypatch):
    """Capturing a global metric snapshot persists; history call
    returns it ordered newest-first."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)

    # Drive metrics
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    conv.close_conversation(
        conversation_id=inquiry.id, closed_by="alice", resolution="answered",
    )

    snap = conv.capture_metric_snapshot()
    assert snap["thread_id"] is None  # global
    assert snap["snapshot"]["conversations_opened"] == 1
    assert snap["snapshot"]["conversations_closed_by_resolution"]["answered"] == 1

    history = conv.get_metric_history()
    assert len(history) == 1
    assert history[0]["id"] == snap["id"]


def test_per_thread_snapshot_isolated_from_global(tmp_path, monkeypatch):
    """Thread-scoped snapshots are stored separately from global ones."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)

    conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    conv.capture_metric_snapshot()  # global
    conv.capture_metric_snapshot(discord_thread_id=thread.discord_thread_id)  # per-thread

    global_history = conv.get_metric_history()
    thread_history = conv.get_metric_history(discord_thread_id=thread.discord_thread_id)
    assert len(global_history) == 1
    assert len(thread_history) == 1
    assert global_history[0]["thread_id"] is None
    assert thread_history[0]["thread_id"] == thread.id


def test_latency_stats_compute_from_closed_conversations(tmp_path, monkeypatch):
    """compute_latency_stats returns avg/min/max time-to-close per
    kind. Backdated created_at is used to fake elapsed time."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat, conv = _build(modules)
    thread = _open_thread(chat)

    inq = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    prop = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal", title="p", opener_actor="alice",
        ),
    )
    # Backdate inquiry by 5min, proposal by 2h
    now = datetime.now(timezone.utc)
    with db.session_scope() as session:
        inq_row = session.get(chat_models.ChatConversationModel, inq.id)
        inq_row.created_at = now - timedelta(minutes=5)
        prop_row = session.get(chat_models.ChatConversationModel, prop.id)
        prop_row.created_at = now - timedelta(hours=2)

    conv.close_conversation(conversation_id=inq.id, closed_by="alice", resolution="answered")
    conv.close_conversation(conversation_id=prop.id, closed_by="alice", resolution="accepted")

    stats = conv.compute_latency_stats(
        discord_thread_id=thread.discord_thread_id,
    )
    assert stats["sample_size"] == 2
    assert "inquiry" in stats["by_kind"]
    assert "proposal" in stats["by_kind"]
    # Inquiry was backdated 5min, proposal 2h -- avg should be > 5min
    inq_avg = stats["by_kind"]["inquiry"]["avg"]
    prop_avg = stats["by_kind"]["proposal"]["avg"]
    assert 4 * 60 < inq_avg < 6 * 60  # ~5min
    assert 1.5 * 3600 < prop_avg < 2.5 * 3600  # ~2h
    assert stats["overall"]["count"] == 2.0
