"""PR7: multi-tier idle escalation + soft turn-taking gauge.

Covers:
- tier-1 fires once at >= 1x base idle threshold
- tier-2 fires once at >= 4x base
- tier-3 auto-abandons (close with resolution=abandoned, closed_by=system)
- already-warned conversations never re-fire the same tier
- a single sweep can advance multiple tiers in one call
- unaddressed_speech_count increments only when expected_speaker
  is set and the speaker is neither expected nor addressing anyone
- the gauge resets when expected_speaker changes
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
        "chat_models": chat_models,
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
    return chat_service, conversation_service


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


def _backdate(db, models, conversation_id: str, *, minutes: int) -> None:
    backdated = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with db.session_scope() as session:
        row = session.get(models.ChatConversationModel, conversation_id)
        row.created_at = backdated
        row.last_speech_at = backdated


def test_tier_1_then_tier_2_emit_in_order(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service = _build(modules)

    thread = _open_thread(chat_service)
    base_seconds = 30 * 60  # tier-1 base = 30 minutes
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="Anyone owning the auth refactor?",
            opener_actor="alice",
            addressed_to="bob",
        ),
    )

    # backdate to 35min: tier-1 should fire (1x = 30min); tier-2 should not (4x = 120min)
    _backdate(db, chat_models, inquiry.id, minutes=35)
    flagged = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=base_seconds,
    )
    assert len(flagged) == 1
    assert flagged[0].idle_warning_count == 1
    assert flagged[0].state == "open"  # not abandoned yet

    # immediate re-sweep at the same age must not fire again
    flagged_again = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=base_seconds,
    )
    assert flagged_again == []

    # backdate further to 130min: tier-2 should fire now (4x = 120min)
    _backdate(db, chat_models, inquiry.id, minutes=130)
    flagged_t2 = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=base_seconds,
    )
    assert len(flagged_t2) == 1
    assert flagged_t2[0].idle_warning_count == 2
    assert flagged_t2[0].state == "open"

    # Verify exactly two warning event rows in DB
    with db.session_scope() as session:
        events = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == inquiry.id)
                .where(chat_models.ChatMessageModel.event_kind == "chat.conversation.idle_warning")
                .order_by(chat_models.ChatMessageModel.created_at.asc())
            )
        )
        assert len(events) == 2


def test_tier_3_auto_abandons_with_system_authority(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_models = modules["chat_models"]
    db = modules["db"]
    chat_service, conversation_service = _build(modules)

    thread = _open_thread(chat_service)
    base_seconds = 30 * 60  # tier-3 = 48x = 24h

    # Use a proposal so resolution=abandoned is not in its standard
    # vocabulary -- this also proves the bypass path skips
    # resolution-vocab enforcement.
    proposal = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal",
            title="Adopt evidence-required heartbeats",
            opener_actor="alice",
        ),
    )

    # backdate by 25h to comfortably exceed tier-3 (24h)
    _backdate(db, chat_models, proposal.id, minutes=25 * 60)
    flagged = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=base_seconds,
    )
    # tiers 1 + 2 emitted, then tier-3 auto-abandon
    assert len(flagged) == 1  # the abandoned conv shows up in flagged
    assert flagged[0].state == "closed"
    assert flagged[0].resolution == "abandoned"
    assert flagged[0].closed_by == "system"
    assert flagged[0].idle_warning_count == 3

    with db.session_scope() as session:
        warning_events = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == proposal.id)
                .where(chat_models.ChatMessageModel.event_kind == "chat.conversation.idle_warning")
            )
        )
        closed_events = list(
            session.scalars(
                select(chat_models.ChatMessageModel)
                .where(chat_models.ChatMessageModel.conversation_id == proposal.id)
                .where(chat_models.ChatMessageModel.event_kind == "chat.conversation.closed")
            )
        )
    # tier-1 and tier-2 each emit a warning row before abandon
    assert len(warning_events) == 2
    assert len(closed_events) == 1


def test_unaddressed_speech_count_tracks_off_turn_speakers(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service = _build(modules)

    thread = _open_thread(chat_service)
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="Anyone has the schema migration plan?",
            opener_actor="alice",
            addressed_to="bob",
        ),
    )
    assert inquiry.expected_speaker == "bob"
    assert inquiry.unaddressed_speech_count == 0

    # carol speaks unaddressed -- bob is still expected. bump.
    conversation_service.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="carol",
            kind="claim",
            content="I think I saw the doc",
        ),
    )
    detail = conversation_service.get_conversation(conversation_id=inquiry.id)
    assert detail.conversation.unaddressed_speech_count == 1
    assert detail.conversation.expected_speaker == "bob"

    # dave also chimes in unaddressed -- bump again
    conversation_service.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="dave",
            kind="claim",
            content="me too",
        ),
    )
    detail = conversation_service.get_conversation(conversation_id=inquiry.id)
    assert detail.conversation.unaddressed_speech_count == 2

    # bob (the expected speaker) finally answers -- gauge resets
    conversation_service.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob",
            kind="answer",
            content="Yes the plan is in docs/migration-2026.md",
        ),
    )
    detail = conversation_service.get_conversation(conversation_id=inquiry.id)
    assert detail.conversation.unaddressed_speech_count == 0
    assert detail.conversation.expected_speaker is None  # cleared after expected speaker answered


def test_unaddressed_speech_count_resets_on_new_address(tmp_path, monkeypatch):
    modules = _bootstrap_app(tmp_path, monkeypatch)
    schemas = modules["conversation_schemas_module"]
    chat_service, conversation_service = _build(modules)

    thread = _open_thread(chat_service)
    inquiry = conversation_service.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry",
            title="?",
            opener_actor="alice",
            addressed_to="bob",
        ),
    )
    # carol speaks off-turn
    conversation_service.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="carol", kind="claim", content="..."
        ),
    )
    # alice re-addresses to dave -- gauge must reset to 0. Any speech
    # kind works; only addressed_to drives the redirect.
    conversation_service.submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice",
            kind="question",
            content="actually, dave please",
            addressed_to="dave",
        ),
    )
    detail = conversation_service.get_conversation(conversation_id=inquiry.id)
    assert detail.conversation.expected_speaker == "dave"
    assert detail.conversation.unaddressed_speech_count == 0
