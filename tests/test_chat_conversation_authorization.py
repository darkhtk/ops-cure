"""Authorization tests for the conversation protocol (PR6).

Covers the string-match authority layer:

- ``close_conversation`` rejects ``closed_by`` that is neither opener
  nor owner
- ``transfer_owner`` rejects ``by_actor`` that is neither opener nor
  current owner
- after handoff, the new owner can close (proves owner authority is
  evaluated against the *current* owner_actor, not a frozen value)
- task complete/fail still auto-closes via the coordinator's
  ``bypass_task_guard`` path (bypass is the lease-token authority,
  not a rule weakness)
"""

from __future__ import annotations

import asyncio
import sys

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

    db.init_db()

    return {
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "conversation_schemas_module": conversation_schemas_module,
        "task_coordinator_module": task_coordinator_module,
        "presence_module": presence_module,
        "approvals_module": approvals_module,
        "remote_task_service_module": remote_task_service_module,
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
    return chat_service, conversation_service, coordinator


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


def test_close_by_unauthorized_actor_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="What's the rotation policy?",
            opener_actor="alice",
        ),
    )
    # mallory has not opened, owns nothing
    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.close_conversation(
            conversation_id=inquiry.id,
            closed_by="mallory",
            resolution="dropped",
        )

    # opener can still close — sanity
    closed = conversation_service.close_conversation(
        conversation_id=inquiry.id,
        closed_by="alice",
        resolution="answered",
    )
    assert closed.state == "closed"


def test_close_by_owner_works_when_distinct_from_opener(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)
    proposal = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal",
            title="Adopt evidence-required heartbeats",
            opener_actor="alice",
            owner_actor="bob",  # explicit different owner at open time
        ),
    )
    closed = conversation_service.close_conversation(
        conversation_id=proposal.id,
        closed_by="bob",  # owner, not opener
        resolution="accepted",
    )
    assert closed.state == "closed"


def test_handoff_by_unauthorized_actor_rejected(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)
    proposal = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal",
            title="something",
            opener_actor="alice",
            owner_actor="alice",
        ),
    )
    with pytest.raises(conv_module.ChatConversationStateError):
        conversation_service.transfer_owner(
            conversation_id=proposal.id,
            by_actor="mallory",
            new_owner="bob",
        )


def test_handoff_chain_authorizes_new_owner_to_close(tmp_path, monkeypatch):
    """After alice -> bob handoff, bob is now the current owner and should
    be able to close. The owner authority is evaluated against the live
    owner_actor field, not frozen at open time."""
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service, _ = _build(modules)

    thread = _open_thread(chat_service)
    proposal = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal",
            title="something",
            opener_actor="alice",
            owner_actor="alice",
        ),
    )
    conversation_service.transfer_owner(
        conversation_id=proposal.id,
        by_actor="alice",
        new_owner="bob",
    )
    closed = conversation_service.close_conversation(
        conversation_id=proposal.id,
        closed_by="bob",
        resolution="accepted",
    )
    assert closed.state == "closed"
    assert closed.owner_actor == "bob"


def test_task_auto_close_bypasses_authorization_via_lease(tmp_path, monkeypatch):
    """When a task settles, ChatTaskCoordinator closes the bound
    conversation with bypass_task_guard=True. The auth gate must not
    fire on this path because the lease_token already authorized the
    actor at the RemoteTaskService level."""
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service, coordinator = _build(modules)

    thread = _open_thread(chat_service)
    task_conv = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task",
            title="bound task",
            opener_actor="alice",
            objective="do the work",
        ),
    )
    claimed = coordinator.claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    lease_token = claimed.task["current_assignment"]["lease_token"]
    completed = coordinator.complete(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskCompleteRequest(
            actor_name="codex-pca",  # not opener, not pre-existing owner
            lease_token=lease_token,
            summary="done",
        ),
    )
    assert completed.conversation.state == "closed"
    assert completed.conversation.resolution == "completed"
