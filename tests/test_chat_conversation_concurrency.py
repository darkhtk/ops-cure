"""PR10: concurrency / lease / contention coverage.

These tests are sequential rather than threaded -- the lease_token is
the canonical lock and we only need to verify "second caller after the
first has won" semantics, not real OS-thread races.

Covered:
- claim contention: actor A claims, actor B's claim attempt is
  rejected while A's lease is active
- lease expired: backdating the lease lets actor B take over
- wrong lease_token on heartbeat is rejected
- closing a task-bound conversation via coordinator.complete then
  trying to complete again hits the auto-closed guard
- closing an already-closed inquiry/proposal a second time is
  rejected with the standard already-closed error
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest

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
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.services.remote_task_service as remote_task_service_module
    import app.models as models_module

    db.init_db()

    return {
        "db": db,
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "conversation_schemas_module": conversation_schemas_module,
        "task_coordinator_module": task_coordinator_module,
        "presence_module": presence_module,
        "approvals_module": approvals_module,
        "remote_task_service_module": remote_task_service_module,
        "models_module": models_module,
    }


def _build(modules):
    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(
        thread_manager=thread_manager,
    )
    presence = modules["presence_module"].PresenceService()
    approvals = modules["approvals_module"].KernelApprovalService()
    remote_task = modules["remote_task_service_module"].RemoteTaskService(
        presence_service=presence,
        kernel_approval_service=approvals,
    )
    conversation_service = modules["conversation_service_module"].ChatConversationService(
        remote_task_service=remote_task,
    )
    coordinator = modules["task_coordinator_module"].ChatTaskCoordinator(
        conversation_service=conversation_service,
        remote_task_service=remote_task,
    )
    return chat_service, conversation_service, remote_task, coordinator


def _open_thread(chat_service):
    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="collab room",
            topic=None,
            created_by="alice",
        )

    return asyncio.run(scenario())


def _open_task(conversation_service, schemas, thread, *, opener="alice"):
    return conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="some work",
            opener_actor=opener,
            objective="do the work",
        ),
    )


def test_claim_contention_first_actor_wins(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service, _, coordinator = _build(modules)

    thread = _open_thread(chat_service)
    task_conv = _open_task(conversation_service, schemas, thread)

    # actor A claims first
    response_a = coordinator.claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    assert response_a.task["status"] == "claimed"
    assert response_a.conversation.owner_actor == "codex-pca"

    # actor B's claim attempt while A's lease is live must be rejected.
    # The presence service raises (rather than silently overwriting).
    with pytest.raises(Exception):  # noqa: BLE001
        coordinator.claim(
            conversation_id=task_conv.id,
            request=schemas.ChatTaskClaimRequest(actor_name="claude-pcb", lease_seconds=120),
        )

    # confirm A still holds the lease
    detail = conversation_service.get_conversation(conversation_id=task_conv.id)
    assert detail.conversation.owner_actor == "codex-pca"


def test_lease_expired_lets_new_actor_claim(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    db = modules["db"]
    models = modules["models_module"]
    chat_service, conversation_service, _, coordinator = _build(modules)

    thread = _open_thread(chat_service)
    task_conv = _open_task(conversation_service, schemas, thread)

    # actor A claims with a tight lease
    response_a = coordinator.claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=30),
    )
    task_id = task_conv.bound_task_id
    assert task_id is not None

    # Force lease expiration. The kernel ResourceLeaseModel is the
    # authoritative lock; the assignment row only mirrors it. Backdate
    # both so the next claim_resource_lease() call sees no live holder.
    from sqlalchemy import select as _select
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    with db.session_scope() as session:
        assignment_id = response_a.task["current_assignment"]["id"]
        assignment_row = session.get(models.RemoteTaskAssignmentModel, assignment_id)
        assignment_row.lease_expires_at = expired
        assignment_row.status = "released"
        assignment_row.released_at = expired
        lease_row = session.scalar(
            _select(models.ResourceLeaseModel)
            .where(models.ResourceLeaseModel.resource_kind == "remote_task")
            .where(models.ResourceLeaseModel.resource_id == task_id)
        )
        if lease_row is not None:
            lease_row.expires_at = expired
            lease_row.released_at = expired
            lease_row.status = "released"

    # actor B can now take over
    response_b = coordinator.claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="claude-pcb", lease_seconds=60),
    )
    assert response_b.task["status"] == "claimed"
    assert response_b.conversation.owner_actor == "claude-pcb"


def test_heartbeat_with_wrong_lease_token_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service, _, coordinator = _build(modules)

    thread = _open_thread(chat_service)
    task_conv = _open_task(conversation_service, schemas, thread)

    coordinator.claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )

    with pytest.raises(Exception):  # noqa: BLE001
        coordinator.heartbeat(
            conversation_id=task_conv.id,
            request=schemas.ChatTaskHeartbeatRequest(
                actor_name="codex-pca",
                lease_token="not-the-real-token",
                phase="executing",
                summary="trying to fake a heartbeat",
            ),
        )


def test_double_complete_after_auto_close_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service, _, coordinator = _build(modules)

    thread = _open_thread(chat_service)
    task_conv = _open_task(conversation_service, schemas, thread)

    claimed = coordinator.claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    lease_token = claimed.task["current_assignment"]["lease_token"]
    # PR-hardening: complete now requires at least one evidence row.
    coordinator.add_evidence(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex-pca", lease_token=lease_token,
            kind="file_write", summary="did the work",
        ),
    )
    coordinator.complete(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskCompleteRequest(
            actor_name="codex-pca",
            lease_token=lease_token,
            summary="done",
        ),
    )

    # second complete: lease is gone, conversation is closed
    with pytest.raises(Exception):  # noqa: BLE001
        coordinator.complete(
            conversation_id=task_conv.id,
            request=schemas.ChatTaskCompleteRequest(
                actor_name="codex-pca",
                lease_token=lease_token,
                summary="done again?",
            ),
        )


def test_double_close_inquiry_rejected(tmp_path, monkeypatch):
    """Sequential close contention on a non-task conversation. Second
    call surfaces the existing 'already closed' state error."""
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]
    chat_service, conversation_service, _, _ = _build(modules)

    thread = _open_thread(chat_service)
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="?",
            opener_actor="alice",
        ),
    )
    conversation_service.close_conversation(
        conversation_id=inquiry.id,
        closed_by="alice",
        resolution="answered",
    )
    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.close_conversation(
            conversation_id=inquiry.id,
            closed_by="alice",
            resolution="dropped",
        )
