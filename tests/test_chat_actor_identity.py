"""PR13: actor identity binding via authorizer callback.

Closes failure-mode GAP #06 in scripts/failure_mode_scenarios.py.
Previously the bridge token authenticated the *caller* but speech
accepted any actor_name string -- mallory could spoof alice. With
an authorizer wired, every conversation-level method that takes an
actor_name validates it before persisting.
"""

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
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.services.remote_task_service as remote_task_service_module

    db.init_db()

    return {
        "chat_service_module": chat_service_module,
        "conversation_service_module": conversation_service_module,
        "conversation_schemas_module": conversation_schemas_module,
        "presence_module": presence_module,
        "approvals_module": approvals_module,
        "remote_task_service_module": remote_task_service_module,
    }


def _open_thread(chat_service):
    async def go():
        return await chat_service.create_chat_thread(
            guild_id="g", parent_channel_id="p", title="t",
            topic=None, created_by="alice",
        )
    return asyncio.run(go())


def test_authorizer_rejects_actor_spoofing(tmp_path, monkeypatch):
    """When wired, an authorizer that allows only alice rejects
    speech submitted with actor_name='mallory'."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]

    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(
        thread_manager=thread_manager,
    )
    presence = modules["presence_module"].PresenceService()
    approvals = modules["approvals_module"].KernelApprovalService()
    remote_task = modules["remote_task_service_module"].RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )

    # Fake authorizer: alice may speak; nobody else.
    def authorizer(_caller_ctx, actor_name):
        return actor_name == "alice"

    conversation_service = conv_module.ChatConversationService(
        remote_task_service=remote_task, actor_authorizer=authorizer,
    )

    thread = _open_thread(chat_service)
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )

    # mallory tries to submit speech as themselves -- rejected
    with pytest.raises(conv_module.ChatActorIdentityError):
        conversation_service.submit_speech(
            conversation_id=inquiry.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="mallory", kind="claim", content="...",
            ),
        )

    # mallory tries to spoof as alice -- the bridge has no way to
    # tell apart from a legitimate alice call IF the authorizer is
    # wired token-naive. With token-aware authorizer (caller_ctx
    # carries token id), this would fail. Demonstrated by passing a
    # caller context that flips the rule:
    def token_aware_authorizer(caller_ctx, actor_name):
        # caller_ctx is the token id; alice's token can speak as alice
        if caller_ctx == "alice-token" and actor_name == "alice":
            return True
        return False

    conv2 = conv_module.ChatConversationService(
        remote_task_service=remote_task,
        actor_authorizer=token_aware_authorizer,
    )
    # legit alice with her token -- accepted
    conv2.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="legit",
        ),
        caller_context="alice-token",
    )
    # mallory's token tries to act as alice -- rejected
    with pytest.raises(conv_module.ChatActorIdentityError):
        conv2.submit_speech(
            conversation_id=inquiry.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="alice", kind="claim", content="(mallory pretending)",
            ),
            caller_context="mallory-token",
        )


def test_authorizer_unset_preserves_back_compat(tmp_path, monkeypatch):
    """Without an authorizer wired, all actor_names pass -- so
    existing call sites keep working without identity wiring."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]

    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(
        thread_manager=thread_manager,
    )
    presence = modules["presence_module"].PresenceService()
    approvals = modules["approvals_module"].KernelApprovalService()
    remote_task = modules["remote_task_service_module"].RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )
    conversation_service = modules["conversation_service_module"].ChatConversationService(
        remote_task_service=remote_task,  # no actor_authorizer -- back-compat
    )

    thread = _open_thread(chat_service)
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    # Even mallory is fine when no authorizer is set
    conversation_service.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="mallory", kind="claim", content="back-compat path",
        ),
    )


def test_authorizer_skipped_on_bypass_path(tmp_path, monkeypatch):
    """system auto-close path uses bypass_task_guard=True; the
    actor authorizer must NOT fire there because the closer is
    'system' which the authorizer wouldn't know about."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    conv_module = modules["conversation_service_module"]

    thread_manager = FakeThreadManager()
    chat_service = modules["chat_service_module"].ChatBehaviorService(
        thread_manager=thread_manager,
    )
    presence = modules["presence_module"].PresenceService()
    approvals = modules["approvals_module"].KernelApprovalService()
    remote_task = modules["remote_task_service_module"].RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )
    # Strict authorizer that rejects everything
    conversation_service = conv_module.ChatConversationService(
        remote_task_service=remote_task,
        actor_authorizer=lambda _ctx, _name: False,
    )

    thread = _open_thread(chat_service)
    # Direct open won't work (alice rejected) -- bypass authorizer for setup
    # by calling underlying service mode
    # Simpler: just verify the bypass path on close itself
    # Use a non-strict service to set up, then strict-close via bypass
    setup_conv = conv_module.ChatConversationService(remote_task_service=remote_task)
    proposal = setup_conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal", title="p", opener_actor="alice",
        ),
    )
    # Now use the strict service to close with bypass_task_guard=True
    # -- should succeed despite the authorizer's "reject all" rule.
    closed = conversation_service.close_conversation(
        conversation_id=proposal.id,
        closed_by="system",
        resolution="abandoned",
        bypass_task_guard=True,
    )
    assert closed.state == "closed"
    assert closed.resolution == "abandoned"
