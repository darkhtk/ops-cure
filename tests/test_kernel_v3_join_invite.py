"""v3 phase 2.5 — JOIN / INVITE membership acts."""
from __future__ import annotations

import sys
import uuid

import pytest

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    db.init_db()
    from app.behaviors.chat.conversation_service import (
        ChatConversationService, ChatConversationStateError,
    )
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.kernel.v2 import V2Repository
    from app.kernel.v2 import contract as v2_contract
    return locals()


def _thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d-join", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def test_self_or_invite_default_lets_anyone_join(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
        ),
    )
    # bob self-joins (default join_policy=self_or_invite) — succeeds
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="bob", kind="join", content="@bob joins",
        ),
    )
    # bob is now a participant
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        participants = repo.list_participants(s, operation_id=v1.v2_operation_id)
        from app.kernel.v2.models import ActorV2Model
        actor_handles = {
            s.get(ActorV2Model, p.actor_id).handle for p in participants
        }
        assert "@bob" in actor_handles


def test_invite_only_rejects_uninvited_self_join(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            policy={"join_policy": m["v2_contract"].JOIN_POLICY_INVITE_ONLY},
        ),
    )
    # bob (no prior invite) tries to self-join → rejected
    with pytest.raises(m["ChatConversationStateError"], match="invite_only"):
        chat.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                actor_name="bob", kind="join", content="@bob joins",
            ),
        )


def test_invite_then_join_under_invite_only(tmp_path, monkeypatch):
    """invite_only flow: alice invites bob, bob then self-joins."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            policy={"join_policy": m["v2_contract"].JOIN_POLICY_INVITE_ONLY},
        ),
    )
    # alice (participant, opener) invites bob
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="invite", content="inviting @bob",
            addressed_to="bob",
        ),
    )
    # bob now has role=addressed, so join is admissible
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="bob", kind="join", content="thanks for the invite",
        ),
    )


def test_invite_from_non_participant_rejected(tmp_path, monkeypatch):
    """An outsider cannot bootstrap themselves into invite_only by
    self-inviting — only existing participants may invite."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            policy={"join_policy": m["v2_contract"].JOIN_POLICY_INVITE_ONLY},
        ),
    )
    with pytest.raises(m["ChatConversationStateError"], match="existing participant"):
        chat.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                actor_name="mallory", kind="invite",
                content="inviting @mallory", addressed_to="mallory",
            ),
        )


def test_open_join_policy_lets_anyone_join_without_invite(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            policy={"join_policy": m["v2_contract"].JOIN_POLICY_OPEN},
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="bob", kind="join", content="open op, joining",
        ),
    )
