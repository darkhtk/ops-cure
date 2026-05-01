"""PR12: in-memory metrics + room health endpoint coverage.

The metrics surface is intentionally tiny: a plain dataclass with
counters that increment on each protocol transition. Health endpoint
combines those with a live DB-derived per-thread view.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

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
    import app.behaviors.chat.task_coordinator as task_coordinator_module
    import app.behaviors.chat.metrics as metrics_module
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
        "task_coordinator_module": task_coordinator_module,
        "metrics_module": metrics_module,
        "chat_models": chat_models,
        "presence_module": presence_module,
        "approvals_module": approvals_module,
        "remote_task_service_module": remote_task_service_module,
    }


def _build(modules):
    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(thread_manager=thread_manager)
    presence = modules["presence_module"].PresenceService()
    approvals = modules["approvals_module"].KernelApprovalService()
    remote_task = modules["remote_task_service_module"].RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )
    metrics = modules["metrics_module"].ChatRoomMetrics()
    conversation_service = modules["conversation_service_module"].ChatConversationService(
        remote_task_service=remote_task, metrics=metrics,
    )
    coordinator = modules["task_coordinator_module"].ChatTaskCoordinator(
        conversation_service=conversation_service, remote_task_service=remote_task,
    )
    return chat_service, conversation_service, coordinator, metrics


def _open_thread(chat_service):
    async def go():
        return await chat_service.create_chat_thread(
            guild_id="g", parent_channel_id="p", title="t",
            topic=None, created_by="alice",
        )
    return asyncio.run(go())


def test_metrics_count_lifecycle_transitions(tmp_path, monkeypatch):
    """Every public protocol transition bumps the matching counter."""
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service, coordinator, metrics = _build(modules)

    thread = _open_thread(chat_service)

    # 1 inquiry: open + speech + close
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    conversation_service.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="question", content="?",
        ),
    )
    conversation_service.close_conversation(
        conversation_id=inquiry.id, closed_by="alice", resolution="answered",
    )

    # 1 proposal: open + handoff + close
    proposal = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal", title="p", opener_actor="alice",
        ),
    )
    conversation_service.transfer_owner(
        conversation_id=proposal.id, by_actor="alice", new_owner="bob",
    )
    conversation_service.close_conversation(
        conversation_id=proposal.id, closed_by="bob", resolution="accepted",
    )

    # 1 task: open + claim + heartbeat + evidence + complete
    task = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="x",
        ),
    )
    claimed = coordinator.claim(
        conversation_id=task.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )
    lease = claimed.task["current_assignment"]["lease_token"]
    coordinator.heartbeat(
        conversation_id=task.id,
        request=schemas.ChatTaskHeartbeatRequest(
            actor_name="codex", lease_token=lease, phase="executing",
        ),
    )
    coordinator.add_evidence(
        conversation_id=task.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex", lease_token=lease,
            kind="file_write", summary="...",
        ),
    )
    coordinator.complete(
        conversation_id=task.id,
        request=schemas.ChatTaskCompleteRequest(
            actor_name="codex", lease_token=lease, summary="ok",
        ),
    )

    snap = metrics.snapshot()
    assert snap["conversations_opened"] == 3
    # 3 closures: answered (inquiry), accepted (proposal), completed (task auto-close)
    closed = snap["conversations_closed_by_resolution"]
    assert closed["answered"] == 1
    assert closed["accepted"] == 1
    assert closed["completed"] == 1
    assert snap["handoffs"] == 1
    # 1 question speech act
    assert snap["speech_by_kind"]["question"] == 1
    # task counters
    assert snap["task"]["claimed"] == 1
    assert snap["task"]["heartbeat"] == 1
    assert snap["task"]["evidence"] == 1
    assert snap["task"]["completed"] == 1
    assert snap["task"]["failed"] == 0


def test_metrics_count_idle_warnings_per_tier(tmp_path, monkeypatch):
    """Each tier-N warning bumps idle_warnings_by_tier[N]."""
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service, _, metrics = _build(modules)

    thread = _open_thread(chat_service)
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    # backdate to 2.5h to fire tier-1 + tier-2 in one sweep
    backdated = datetime.now(timezone.utc) - timedelta(minutes=150)
    with db.session_scope() as session:
        row = session.get(chat_models.ChatConversationModel, inquiry.id)
        row.created_at = backdated
        row.last_speech_at = backdated

    conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )

    snap = metrics.snapshot()
    assert snap["idle_warnings_by_tier"]["1"] == 1
    assert snap["idle_warnings_by_tier"]["2"] == 1


def test_get_room_health_returns_live_view(tmp_path, monkeypatch):
    """Health endpoint reports open count, idle candidates, expected
    speakers, bound active tasks + global metrics snapshot."""
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service, coordinator, metrics = _build(modules)

    thread = _open_thread(chat_service)

    # 1 inquiry pending an answer from bob
    pending = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="q", opener_actor="alice", addressed_to="bob",
        ),
    )
    # 1 task in flight
    task = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="x",
        ),
    )
    coordinator.claim(
        conversation_id=task.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )

    # backdate the inquiry so it qualifies as idle_candidate (>30min)
    backdated = datetime.now(timezone.utc) - timedelta(minutes=35)
    with db.session_scope() as session:
        row = session.get(chat_models.ChatConversationModel, pending.id)
        row.created_at = backdated
        row.last_speech_at = backdated

    snap = conversation_service.get_room_health(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )

    # 3 open: inquiry + task + general
    assert snap["open_conversations"] == 3
    # only inquiry (non-general, past tier-1, not yet warned) is a candidate
    assert snap["idle_candidates"] == 1
    # bob is the expected speaker on the inquiry; codex is the task owner
    # (task uses owner_actor not expected_speaker until address fires)
    assert "bob" in snap["expected_speakers"]
    # exactly one task conversation has a bound_task_id
    assert snap["bound_active_tasks"] == 1
    # metrics are present and reflect the actions taken above
    assert snap["metrics"]["conversations_opened"] == 2  # inquiry + task (general bootstrap doesn't bump)
    assert snap["metrics"]["task"]["claimed"] == 1
