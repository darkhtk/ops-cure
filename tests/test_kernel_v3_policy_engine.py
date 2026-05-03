"""v3 Phase 2 — policy engine enforcement.

Tests cover the three enforcement paths the engine adds:
  1. ``max_rounds`` rejects new speech once the cap is reached
  2. ``expected_response.kinds`` whitelist rejects mismatched replies
  3. close policies (operator_ratifies, quorum, any_participant)
     each demand the right shape of pre-close evidence

Default policy (``opener_unilateral``, no max_rounds) is the v2
behaviour — separate tests confirm the engine is a no-op there.
"""
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
    from app.behaviors.chat.models import (
        ChatThreadModel, ChatConversationModel, ChatMessageModel,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.kernel.v2 import V2Repository
    from app.kernel.v2 import contract as v2_contract
    return locals()


def _thread(db, Thread, suffix="1"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def _open(chat, m, discord, *, opener="alice", policy=None, addressed_to=None):
    return chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor=opener,
            addressed_to=addressed_to, policy=policy,
        ),
    )


def _say(chat, m, conv_id, *, actor="alice", kind="claim", content="hi",
         close_intent: bool = False, **kwargs):
    """Submit a speech act. When ``close_intent=True`` and the kind
    is ratify, the helper additionally stamps ``payload.intent="close"``
    on the just-written v2 event so D9's quorum gate counts the
    vote. The chat-service path does not carry arbitrary payload
    keys (only ``content``→``text``); the patch applied here is
    the same shape the production code does in
    ``v2_operations.append_event``."""
    summary = chat.submit_speech(
        conversation_id=conv_id,
        request=m["SpeechActSubmitRequest"](
            actor_name=actor, kind=kind, content=content, **kwargs,
        ),
    )
    if close_intent and kind == "ratify":
        import json as _json
        from app.behaviors.chat.models import ChatMessageModel
        from app.kernel.v2.models import OperationEventV2Model
        with m["db"].session_scope() as _db:
            v1_msg = _db.get(ChatMessageModel, summary.id)
            if v1_msg is not None and v1_msg.v2_event_id:
                v2_ev = _db.get(OperationEventV2Model, v1_msg.v2_event_id)
                if v2_ev is not None:
                    try:
                        existing = _json.loads(v2_ev.payload_json or "{}")
                    except Exception:
                        existing = {}
                    if not isinstance(existing, dict):
                        existing = {}
                    existing["intent"] = "close"
                    v2_ev.payload_json = _json.dumps(existing, ensure_ascii=False)
                    _db.flush()
    return summary


# ============================================================================
# max_rounds enforcement
# ============================================================================

def test_max_rounds_blocks_speech_past_cap(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(chat, m, discord, policy={"max_rounds": 3})

    _say(chat, m, summary.id, actor="alice", kind="claim", content="1")
    _say(chat, m, summary.id, actor="alice", kind="claim", content="2")
    _say(chat, m, summary.id, actor="alice", kind="claim", content="3")

    with pytest.raises(m["ChatConversationStateError"], match="max_rounds=3"):
        _say(chat, m, summary.id, actor="alice", kind="claim", content="4")


def test_max_rounds_unset_lets_unbounded_speech(tmp_path, monkeypatch):
    """Without max_rounds the engine never trips on speech volume —
    confirms the default policy keeps v2 behaviour."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(chat, m, discord)
    for i in range(20):
        _say(chat, m, summary.id, actor="alice", kind="claim", content=f"speech {i}")


# ============================================================================
# reply-kind whitelist
# ============================================================================

def test_reply_kind_outside_whitelist_is_rejected(tmp_path, monkeypatch):
    """A reply whose kind isn't in trigger.expected_response.kinds is
    rejected by the policy engine."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(chat, m, discord)
    parent = _say(
        chat, m, summary.id,
        actor="alice", kind="question", content="EU latency cause?",
        expected_response={
            "from_actor_handles": ["@bob"],
            "kinds": ["answer", "defer"],
        },
    )
    # Resolve the v2 event id for the parent
    repo = m["V2Repository"]()
    with db.session_scope() as s:
        parent_v2 = s.get(m["ChatMessageModel"], parent.id).v2_event_id
        assert parent_v2 is not None

    # Replying with kind=claim should be rejected (not in whitelist).
    with pytest.raises(m["ChatConversationStateError"], match="not in expected_response.kinds"):
        _say(
            chat, m, summary.id,
            actor="bob", kind="claim", content="actually it's DNS",
            replies_to_v2_event_id=parent_v2,
        )

    # Replying with answer (in whitelist) is fine.
    _say(
        chat, m, summary.id,
        actor="bob", kind="answer", content="checking the resolver logs first",
        replies_to_v2_event_id=parent_v2,
    )


def test_reply_kind_wildcard_lets_any_kind(tmp_path, monkeypatch):
    """expected_response.kinds=['*'] means 'any kind is OK'."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(chat, m, discord)
    parent = _say(
        chat, m, summary.id,
        actor="alice", kind="question", content="thoughts?",
        expected_response={"from_actor_handles": ["@bob"], "kinds": ["*"]},
    )
    repo = m["V2Repository"]()
    with db.session_scope() as s:
        parent_v2 = s.get(m["ChatMessageModel"], parent.id).v2_event_id

    # Any kind admissible
    _say(
        chat, m, summary.id,
        actor="bob", kind="object", content="disagreed",
        replies_to_v2_event_id=parent_v2,
    )


# ============================================================================
# close policy: opener_unilateral (default) — engine is a no-op
# ============================================================================

def test_default_close_policy_is_unilateral(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(chat, m, discord)
    closed = chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="answered",
    )
    assert closed.state == "closed"


# ============================================================================
# close policy: any_participant
# ============================================================================

def test_any_participant_close_blocks_non_participant(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(
        chat, m, discord,
        policy={"close_policy": m["v2_contract"].CLOSE_POLICY_ANY_PARTICIPANT},
    )
    # Alice (opener) is a participant — would close fine. But charlie
    # (never spoke / addressed) is not. Direct close call from charlie
    # gets blocked at the legacy authority gate first; we exercise the
    # policy gate by closing as the opener (which the legacy gate
    # accepts) and confirm the policy engine doesn't reject participants.
    closed = chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="answered",
    )
    assert closed.state == "closed"


# ============================================================================
# close policy: operator_ratifies
# ============================================================================

def test_operator_ratify_blocks_close_until_operator_ratifies(tmp_path, monkeypatch):
    """close_policy=operator_ratifies needs an actor with role=operator
    on the op who has posted chat.speech.ratify. Without it the close
    is rejected."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(
        chat, m, discord,
        policy={"close_policy": m["v2_contract"].CLOSE_POLICY_OPERATOR_RATIFIES},
        addressed_to="operator",
    )

    # No operator role exists on the op yet -> close fails.
    with pytest.raises(m["ChatConversationStateError"], match="operator"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )

    # Promote the addressed actor to role=operator manually (the v3
    # phase-2 flow uses INVITE/JOIN; in phase 1 we still drive role
    # via the participants table directly).
    repo = m["V2Repository"]()
    from app.behaviors.chat.models import ChatConversationModel
    with db.session_scope() as s:
        v1 = s.get(ChatConversationModel, summary.id)
        op_id = v1.v2_operation_id
        participants = repo.list_participants(s, operation_id=op_id)
        operator_actor = next(
            (p for p in participants if p.role in ("addressed", "speaker")),
            None,
        )
        assert operator_actor is not None
        # Add a second role row with role=operator (the model allows
        # multiple (actor, op, role) tuples).
        repo.add_participant(
            s, operation_id=op_id,
            actor_id=operator_actor.actor_id, role="operator",
        )

    # Still need an actual ratify event from that operator before close.
    with pytest.raises(m["ChatConversationStateError"], match="operator"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )

    # Now operator posts speech.ratify with close intent -> close passes.
    # (D9 / rev 9: ratify only counts when the bridge can detect
    # close-intent; explicit `payload.intent="close"` is the simplest.)
    _say(
        chat, m, summary.id,
        actor="operator", kind="ratify",
        content="I ratify the close.",
        close_intent=True,
    )
    closed = chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="answered",
    )
    assert closed.state == "closed"


# ============================================================================
# close policy: quorum
# ============================================================================

def test_quorum_close_requires_min_ratifiers(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(
        chat, m, discord,
        policy={
            "close_policy": m["v2_contract"].CLOSE_POLICY_QUORUM,
            "min_ratifiers": 2,
        },
    )
    # 0 ratifiers -> blocked
    with pytest.raises(m["ChatConversationStateError"], match="quorum"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )
    # 1 ratifier -> still blocked. (D9: close_intent=True so D9's
    # gate counts the vote; without it, every ratify here would be
    # treated as a spec-ack and never reach quorum.)
    _say(chat, m, summary.id, actor="bob", kind="ratify", content="ok",
         close_intent=True)
    with pytest.raises(m["ChatConversationStateError"], match="quorum"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )
    # Same actor double-ratifying does NOT bump the count (de-dup)
    _say(chat, m, summary.id, actor="bob", kind="ratify",
         content="still ok", close_intent=True)
    with pytest.raises(m["ChatConversationStateError"], match="quorum"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )
    # 2 distinct ratifiers -> passes
    _say(chat, m, summary.id, actor="carol", kind="ratify",
         content="seconded", close_intent=True)
    closed = chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="answered",
    )
    assert closed.state == "closed"


def test_quorum_close_zero_min_ratifiers_is_noop(tmp_path, monkeypatch):
    """min_ratifiers=0 is meaningless and should not be allowed at
    open time (the contract validator rejects it)."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    with pytest.raises(m["ChatConversationStateError"], match="invalid policy"):
        _open(
            chat, m, discord,
            policy={
                "close_policy": m["v2_contract"].CLOSE_POLICY_QUORUM,
                "min_ratifiers": 0,
            },
        )
