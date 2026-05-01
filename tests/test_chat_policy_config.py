"""PR19: ChatPolicyConfig allows per-deployment threshold tuning."""

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


def _open_thread(chat):
    async def go():
        return await chat.create_chat_thread(
            guild_id="g", parent_channel_id="p", title="t",
            topic=None, created_by="alice",
        )
    return asyncio.run(go())


def test_custom_over_speech_threshold(tmp_path, monkeypatch):
    """A room configured with over_speech_threshold=2 fires the
    over_speech event after 2 off-turn speeches instead of 5."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    conv_module = modules["conversation_service_module"]

    tm = FakeThreadManager()
    chat = modules["chat_service_module"].ChatBehaviorService(thread_manager=tm)
    presence = modules["presence"].PresenceService()
    approvals = modules["approvals"].KernelApprovalService()
    remote = modules["remote_task"].RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )
    policy = conv_module.ChatPolicyConfig(over_speech_threshold=2)
    conv = conv_module.ChatConversationService(
        remote_task_service=remote, policy=policy,
    )

    thread = _open_thread(chat)
    inquiry = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice", addressed_to="bob",
        ),
    )
    # 2 off-turn speeches = threshold reached; over_speech event fires
    for i in range(2):
        conv.submit_speech(
            conversation_id=inquiry.id,
            request=schemas.SpeechActSubmitRequest(
                actor_name="claude", kind="claim", content=f"chiming in {i}",
            ),
        )
    from sqlalchemy import select, func
    with db.session_scope() as session:
        over_count = session.scalar(
            select(func.count())
            .select_from(chat_models.ChatMessageModel)
            .where(chat_models.ChatMessageModel.conversation_id == inquiry.id)
            .where(chat_models.ChatMessageModel.event_kind == "chat.conversation.over_speech")
        ) or 0
    assert over_count == 1


def test_custom_tier_multipliers(tmp_path, monkeypatch):
    """Custom tier multipliers (1/2/4 instead of 1/4/48) make tier-3
    fire much sooner. Useful for incident rooms."""
    modules = _bootstrap(tmp_path, monkeypatch)
    schemas = modules["schemas"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    conv_module = modules["conversation_service_module"]

    tm = FakeThreadManager()
    chat = modules["chat_service_module"].ChatBehaviorService(thread_manager=tm)
    presence = modules["presence"].PresenceService()
    approvals = modules["approvals"].KernelApprovalService()
    remote = modules["remote_task"].RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )
    policy = conv_module.ChatPolicyConfig(
        tier_1_multiplier=1, tier_2_multiplier=2, tier_3_multiplier=4,
    )
    conv = conv_module.ChatConversationService(
        remote_task_service=remote, policy=policy,
    )

    thread = _open_thread(chat)
    proposal = conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal", title="urgent", opener_actor="alice",
        ),
    )
    # backdate by 35min; with tier-3 = 4x = 2h, only tier-1 fires
    backdated = datetime.now(timezone.utc) - timedelta(minutes=35)
    with db.session_scope() as session:
        row = session.get(chat_models.ChatConversationModel, proposal.id)
        row.created_at = backdated
        row.last_speech_at = backdated
    flagged = conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    assert len(flagged) == 1 and flagged[0].idle_warning_count == 1
    assert flagged[0].state == "open"  # not yet abandoned

    # backdate to 130min -> with tier-3=4x=2h, that's >= tier-3 -> auto-abandoned
    backdated2 = datetime.now(timezone.utc) - timedelta(minutes=130)
    with db.session_scope() as session:
        row = session.get(chat_models.ChatConversationModel, proposal.id)
        row.created_at = backdated2
        row.last_speech_at = backdated2
    flagged2 = conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    # tier 2 (60min) and tier 3 (120min) both fire in this sweep
    assert any(f.resolution == "abandoned" for f in flagged2)


def test_invalid_policy_rejected_at_construction(tmp_path, monkeypatch):
    """Non-monotonic multipliers raise immediately."""
    modules = _bootstrap(tmp_path, monkeypatch)
    conv_module = modules["conversation_service_module"]
    with pytest.raises(ValueError):
        conv_module.ChatPolicyConfig(tier_2_multiplier=10, tier_3_multiplier=5)
    with pytest.raises(ValueError):
        conv_module.ChatPolicyConfig(over_speech_threshold=0)
