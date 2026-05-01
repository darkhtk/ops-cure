"""PR14: ChatTaskCoordinator approval / interrupt / note flow."""

from __future__ import annotations

import asyncio
import sys

import pytest
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
    import app.behaviors.chat.models as chat_models
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.services.remote_task_service as remote_task_service_module

    db.init_db()

    return {
        "db": db, "schemas": conversation_schemas_module,
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "task_coordinator_module": task_coordinator_module,
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
    coord = modules["task_coordinator_module"].ChatTaskCoordinator(
        conversation_service=conv, remote_task_service=remote,
    )
    return chat, conv, coord


def _open_thread(chat):
    async def go():
        return await chat.create_chat_thread(
            guild_id="g", parent_channel_id="p", title="t",
            topic=None, created_by="alice",
        )
    return asyncio.run(go())


def _open_task(conv, schemas, thread):
    return conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="x",
        ),
    )


def test_approval_request_and_approve(tmp_path, monkeypatch):
    """Owner requests approval; task moves to blocked_approval.
    Approver resolves approved; task returns to executing
    (auto-promoted because evidence exists)."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv, coord = _build(modules)
    thread = _open_thread(chat)
    task = _open_task(conv, schemas, thread)

    claimed = coord.claim(
        conversation_id=task.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )
    lease = claimed.task["current_assignment"]["lease_token"]
    coord.add_evidence(
        conversation_id=task.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex", lease_token=lease,
            kind="file_write", summary="prepared the destructive op",
        ),
    )
    after_request = coord.request_approval(
        conversation_id=task.id,
        request=schemas.ChatTaskApprovalRequest(
            actor_name="codex", lease_token=lease,
            reason="DELETE 4.2TB; needs human signoff",
            note="dry-run completed; ready to commit",
        ),
    )
    assert after_request.task["status"] == "blocked_approval"

    after_resolve = coord.resolve_approval(
        conversation_id=task.id,
        request=schemas.ChatTaskApprovalResolveRequest(
            resolved_by="alice", resolution="approved",
            note="approved with audit log requirement",
        ),
    )
    # Conversation stays open; task is unblocked
    assert after_resolve.conversation.state == "open"
    assert after_resolve.task["status"] in ("claimed", "executing")


def test_approval_denied_auto_closes_conversation(tmp_path, monkeypatch):
    """When approval is denied, the conversation auto-closes as
    cancelled -- the task can no longer proceed."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv, coord = _build(modules)
    thread = _open_thread(chat)
    task = _open_task(conv, schemas, thread)

    claimed = coord.claim(
        conversation_id=task.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )
    lease = claimed.task["current_assignment"]["lease_token"]
    coord.add_evidence(
        conversation_id=task.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex", lease_token=lease,
            kind="file_write", summary="proposed change",
        ),
    )
    coord.request_approval(
        conversation_id=task.id,
        request=schemas.ChatTaskApprovalRequest(
            actor_name="codex", lease_token=lease, reason="risky migration",
        ),
    )
    after_deny = coord.resolve_approval(
        conversation_id=task.id,
        request=schemas.ChatTaskApprovalResolveRequest(
            resolved_by="alice", resolution="denied",
            note="too much risk this sprint",
        ),
    )
    assert after_deny.conversation.state == "closed"
    assert after_deny.conversation.resolution == "cancelled"


def test_interrupt_keeps_lease_open_for_resume(tmp_path, monkeypatch):
    """Interrupt moves task to interrupted state but keeps the
    conversation open. The same actor (or a recoverer) can resume."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat, conv, coord = _build(modules)
    thread = _open_thread(chat)
    task = _open_task(conv, schemas, thread)

    claimed = coord.claim(
        conversation_id=task.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )
    lease = claimed.task["current_assignment"]["lease_token"]
    coord.add_evidence(
        conversation_id=task.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex", lease_token=lease,
            kind="file_write", summary="started work",
        ),
    )
    after = coord.interrupt(
        conversation_id=task.id,
        request=schemas.ChatTaskInterruptRequest(
            actor_name="codex", lease_token=lease,
            note="pausing to consult human",
        ),
    )
    assert after.task["status"] == "interrupted"
    assert after.conversation.state == "open"  # not auto-closed


def test_add_note_persists_coordination_record(tmp_path, monkeypatch):
    """Notes don't change task state; they're observation-only
    coordination records attached to the bound task."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat, conv, coord = _build(modules)
    thread = _open_thread(chat)
    task = _open_task(conv, schemas, thread)

    coord.claim(
        conversation_id=task.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )
    response = coord.add_note(
        conversation_id=task.id,
        request=schemas.ChatTaskNoteRequest(
            actor_name="bob", kind="crosscheck",
            content="reviewed plan; LGTM but needs runbook entry",
        ),
    )
    assert response.note["kind"] == "crosscheck"
    assert response.conversation.state == "open"

    # Verify chat.task.note event row was emitted
    with db.session_scope() as session:
        rows = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == task.id)
                .where(chat_models.ChatMessageModel.event_kind == "chat.task.note")
            )
        )
        assert len(rows) == 1
