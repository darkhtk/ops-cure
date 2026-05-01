"""Task lifecycle tests for ``Conversation(kind=task)``.

Covers PR2:
- opening kind=task creates a bound RemoteTask row
- claim → heartbeat → evidence → complete drives both the RemoteTask
  state and emits typed ``chat.task.*`` events into the conversation
- complete and fail auto-close the conversation with mirrored
  resolution
- manual close on a task-bound conversation is rejected while the task
  is still active
- task-shaped opens require an objective and a wired RemoteTaskService
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
    import app.behaviors.chat.task_coordinator as task_coordinator_module
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
        "chat_models": chat_models,
        "presence_module": presence_module,
        "approvals_module": approvals_module,
        "remote_task_service_module": remote_task_service_module,
    }


def _build_full_stack(modules):
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
    coordinator = modules["task_coordinator_module"].ChatTaskCoordinator(
        conversation_service=conversation_service,
        remote_task_service=remote_task_service,
    )
    return thread_manager, chat_service, conversation_service, remote_task_service, coordinator


def _open_thread(chat_service, *, title="collab room", topic=None) -> object:
    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title=title,
            topic=topic,
            created_by="alice",
        )

    return asyncio.run(scenario())


def test_open_task_conversation_creates_bound_remote_task(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    _, chat_service, conversation_service, remote_task_service, _ = _build_full_stack(modules)

    thread = _open_thread(chat_service)

    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="Refactor auth middleware",
            opener_actor="alice",
            objective="Replace legacy session token storage",
            success_criteria={"required": ["all tests pass"]},
        ),
    )

    assert opened.kind == "task"
    assert opened.state == "open"
    assert opened.bound_task_id is not None

    task = remote_task_service.get_task(opened.bound_task_id)
    assert task.objective == "Replace legacy session token storage"
    assert task.machine_id == "chat"
    assert task.thread_id == thread.id
    assert task.status == "queued"
    assert task.origin_surface == "chat"


def test_task_lifecycle_claim_heartbeat_evidence_complete(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    _, chat_service, conversation_service, _, coordinator = _build_full_stack(modules)

    thread = _open_thread(chat_service)
    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="Patch transcript optimistic path",
            opener_actor="alice",
            objective="Make transcript feel local",
        ),
    )

    claimed = coordinator.claim(
        conversation_id=opened.id,
        request=schemas.ChatTaskClaimRequest(
            actor_name="codex-homedev",
            lease_seconds=120,
        ),
    )
    assert claimed.task["status"] == "claimed"
    assert claimed.conversation.owner_actor == "codex-homedev"
    assert claimed.conversation.expected_speaker == "codex-homedev"
    assert claimed.task["current_assignment"] is not None
    lease_token = claimed.task["current_assignment"]["lease_token"]

    after_heartbeat = coordinator.heartbeat(
        conversation_id=opened.id,
        request=schemas.ChatTaskHeartbeatRequest(
            actor_name="codex-homedev",
            lease_token=lease_token,
            phase="executing",
            summary="reading transcript module",
            files_read_count=2,
        ),
    )
    assert after_heartbeat.task["latest_heartbeat"]["phase"] == "executing"

    after_evidence = coordinator.add_evidence(
        conversation_id=opened.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex-homedev",
            lease_token=lease_token,
            kind="file_write",
            summary="patched optimistic bubble code path",
            payload={"files": ["public/app.js"]},
        ),
    )
    assert after_evidence.task["status"] == "executing"
    assert after_evidence.task["recent_evidence"][0]["kind"] == "file_write"

    completed = coordinator.complete(
        conversation_id=opened.id,
        request=schemas.ChatTaskCompleteRequest(
            actor_name="codex-homedev",
            lease_token=lease_token,
            summary="optimistic bubble shipped",
        ),
    )
    assert completed.task["status"] == "completed"
    assert completed.conversation.state == "closed"
    assert completed.conversation.resolution == "completed"
    assert completed.conversation.expected_speaker is None

    with db.session_scope() as session:
        events = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == opened.id)
                .order_by(chat_models.ChatMessageModel.created_at.asc()),
            ),
        )
        kinds = [event.event_kind for event in events]
    assert kinds[0] == "chat.conversation.opened"
    assert "chat.task.claimed" in kinds
    assert "chat.task.heartbeat" in kinds
    assert "chat.task.evidence" in kinds
    assert "chat.task.completed" in kinds
    assert kinds[-1] == "chat.conversation.closed"


def test_task_fail_path_auto_closes_conversation(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    _, chat_service, conversation_service, _, coordinator = _build_full_stack(modules)

    thread = _open_thread(chat_service)
    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="Try migration",
            opener_actor="alice",
            objective="run prod migration",
        ),
    )

    claimed = coordinator.claim(
        conversation_id=opened.id,
        request=schemas.ChatTaskClaimRequest(
            actor_name="bob-codex",
            lease_seconds=60,
        ),
    )
    lease_token = claimed.task["current_assignment"]["lease_token"]

    failed = coordinator.fail(
        conversation_id=opened.id,
        request=schemas.ChatTaskFailRequest(
            actor_name="bob-codex",
            lease_token=lease_token,
            error_text="schema mismatch on staging",
        ),
    )
    assert failed.task["status"] == "failed"
    assert failed.conversation.state == "closed"
    assert failed.conversation.resolution == "failed"
    assert failed.conversation.resolution_summary == "schema mismatch on staging"


def test_manual_close_blocked_for_active_task_conversation(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]
    _, chat_service, conversation_service, _, _ = _build_full_stack(modules)

    thread = _open_thread(chat_service)
    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="Active task",
            opener_actor="alice",
            objective="do work",
        ),
    )

    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.close_conversation(
            conversation_id=opened.id,
            closed_by="alice",
            resolution="cancelled",
            summary="changed my mind",
        )


def test_open_task_without_objective_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]
    _, chat_service, conversation_service, _, _ = _build_full_stack(modules)

    thread = _open_thread(chat_service)

    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=schemas.ConversationOpenRequest(
                kind="task",
                title="vague task",
                opener_actor="alice",
            ),
        )


def test_get_conversation_filters_speech_by_kind(tmp_path, monkeypatch):
    """get_conversation(kinds=[...]) returns only matching event_kind
    rows; PR9 ergonomics. Useful for clients that want, e.g., only
    evidence rows or only lifecycle events without chatter."""
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    _, chat_service, conversation_service, _, coordinator = _build_full_stack(modules)

    thread = _open_thread(chat_service)
    opened = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="test",
            opener_actor="alice",
            objective="run something",
        ),
    )
    claimed = coordinator.claim(
        conversation_id=opened.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    lease_token = claimed.task["current_assignment"]["lease_token"]
    coordinator.heartbeat(
        conversation_id=opened.id,
        request=schemas.ChatTaskHeartbeatRequest(
            actor_name="codex-pca", lease_token=lease_token, phase="executing",
        ),
    )
    coordinator.add_evidence(
        conversation_id=opened.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex-pca", lease_token=lease_token,
            kind="file_write", summary="patched",
        ),
    )
    coordinator.complete(
        conversation_id=opened.id,
        request=schemas.ChatTaskCompleteRequest(
            actor_name="codex-pca", lease_token=lease_token, summary="ok",
        ),
    )

    detail_all = conversation_service.get_conversation(conversation_id=opened.id)
    kinds_all = {row.kind for row in detail_all.recent_speech}
    # The kind is stripped of the chat.speech. prefix; lifecycle events
    # keep their full event_kind. Verify several event types are present.
    assert any("opened" in k or "closed" in k for k in kinds_all) or len(kinds_all) >= 3

    detail_evidence_only = conversation_service.get_conversation(
        conversation_id=opened.id,
        kinds=["chat.task.evidence"],
    )
    assert len(detail_evidence_only.recent_speech) == 1
    assert detail_evidence_only.recent_speech[0].kind == "chat.task.evidence"

    detail_lifecycle = conversation_service.get_conversation(
        conversation_id=opened.id,
        kinds=["chat.conversation.opened", "chat.conversation.closed"],
    )
    lifecycle_kinds = [row.kind for row in detail_lifecycle.recent_speech]
    assert "chat.conversation.opened" in lifecycle_kinds
    assert "chat.conversation.closed" in lifecycle_kinds
    # heartbeat / evidence rows should be filtered out
    assert "chat.task.heartbeat" not in lifecycle_kinds
    assert "chat.task.evidence" not in lifecycle_kinds


def test_open_task_without_remote_task_service_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]

    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(
        thread_manager=thread_manager,
    )
    # Note: remote_task_service intentionally omitted.
    conversation_service = conv_module.ChatConversationService()

    thread = _open_thread(chat_service)

    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=schemas.ConversationOpenRequest(
                kind="task",
                title="task without backing service",
                opener_actor="alice",
                objective="should not be created",
            ),
        )
