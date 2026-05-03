"""v3 phase 2 — negative-path / adversarial enforcement tests.

The persona live run only proved that **cooperative** clients respect
the new policy gates. This file pins down that the gates **also reject
misbehavior**: an actor explicitly trying to bypass cap, kind whitelist,
ratify de-dup, or close-policy receives a clean rejection from the
bridge with the right error code.

If any of these tests starts passing-by-accident, a critical safety
property has regressed.
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
         close_intent: bool = True, **kwargs):
    """``close_intent`` defaults True for ratifies in this file's
    tests since the original test semantics presumed every ratify
    was a vote. D9/rev-9 split that meaning; explicit flag keeps
    pre-D9 invariants (quorum dedup etc.) testable.
    """
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


def _v2_event_id(db, m, v1_msg_id):
    with db.session_scope() as s:
        return s.get(m["ChatMessageModel"], v1_msg_id).v2_event_id


# ============================================================================
# 1. Cap bypass attempt
# ============================================================================

def test_cap_bypass_attempts_all_rejected(tmp_path, monkeypatch):
    """When max_rounds=2, the 3rd, 4th, 5th speech all fail. The cap
    binds repeatedly — not just on the boundary."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = _open(chat, m, discord, policy={"max_rounds": 2})
    _say(chat, m, summary.id, content="1")
    _say(chat, m, summary.id, content="2")
    for i in (3, 4, 5):
        with pytest.raises(m["ChatConversationStateError"], match="max_rounds=2"):
            _say(chat, m, summary.id, content=str(i))


# ============================================================================
# 2. Kind whitelist bypass (the case the persona run never actually triggered)
# ============================================================================

def test_kind_whitelist_rejects_off_whitelist_reply(tmp_path, monkeypatch):
    """A misbehaving actor that posts a reply whose kind is NOT in the
    trigger's expected_response.kinds is rejected with a precise error.
    This is the case the persona run couldn't exercise because the
    cooperative LLMs didn't try to bypass."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(chat, m, discord)
    parent = _say(
        chat, m, summary.id,
        actor="alice", kind="question", content="cause?",
        expected_response={
            "from_actor_handles": ["@bob"],
            "kinds": ["object", "question"],
        },
    )
    parent_v2 = _v2_event_id(db, m, parent.id)

    # claim is NOT in whitelist → reject
    with pytest.raises(m["ChatConversationStateError"], match="not in expected_response.kinds"):
        _say(chat, m, summary.id, actor="bob", kind="claim", content="actually DNS",
             replies_to_v2_event_id=parent_v2)
    # propose is NOT in whitelist → reject
    with pytest.raises(m["ChatConversationStateError"], match="not in expected_response.kinds"):
        _say(chat, m, summary.id, actor="bob", kind="propose", content="rotate keys",
             replies_to_v2_event_id=parent_v2)
    # answer is NOT in whitelist either (only object/question allowed)
    with pytest.raises(m["ChatConversationStateError"], match="not in expected_response.kinds"):
        _say(chat, m, summary.id, actor="bob", kind="answer", content="we don't know yet",
             replies_to_v2_event_id=parent_v2)


# ============================================================================
# 3. Self-ratify in quorum (de-dup invariant)
# ============================================================================

def test_quorum_dedups_same_actor_multi_ratify(tmp_path, monkeypatch):
    """An actor that ratifies repeatedly cannot inflate quorum. Quorum
    counts distinct actors, not events."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = _open(
        chat, m, discord,
        policy={
            "close_policy": m["v2_contract"].CLOSE_POLICY_QUORUM,
            "min_ratifiers": 2,
        },
    )
    # bob ratifies five times — should still count as 1 ratifier
    for _ in range(5):
        _say(chat, m, summary.id, actor="bob", kind="ratify", content="me again")
    with pytest.raises(m["ChatConversationStateError"], match="quorum"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )
    # carol ratifies once — quorum reached, close passes
    _say(chat, m, summary.id, actor="carol", kind="ratify", content="seconded")
    closed = chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="answered",
    )
    assert closed.state == "closed"


# ============================================================================
# 4. Non-operator ratify under operator_ratifies
# ============================================================================

def test_operator_ratifies_ignores_non_operator_ratify(tmp_path, monkeypatch):
    """Under operator_ratifies, a ratify from a non-operator participant
    does NOT satisfy the close gate. Only the operator role's ratify
    counts."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = _open(
        chat, m, discord,
        policy={"close_policy": m["v2_contract"].CLOSE_POLICY_OPERATOR_RATIFIES},
    )
    # bob (no role=operator) ratifies — close still blocked
    _say(chat, m, summary.id, actor="bob", kind="ratify", content="i think yes")
    with pytest.raises(m["ChatConversationStateError"], match="operator"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )


# ============================================================================
# 5. Closed-op rejects further speech
# ============================================================================

def test_closed_op_rejects_speech(tmp_path, monkeypatch):
    """After close, no new speech may be posted. Validates that a
    delayed event from a slow agent cannot smuggle into a closed op."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = _open(chat, m, discord)
    _say(chat, m, summary.id, content="hello")
    chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="answered",
    )
    with pytest.raises(m["ChatConversationStateError"], match="closed"):
        _say(chat, m, summary.id, content="late reply")


# ============================================================================
# 6. Cap counts only speech, not lifecycle
# ============================================================================

def test_max_rounds_does_not_count_lifecycle_events(tmp_path, monkeypatch):
    """conversation.opened / conversation.closed do NOT count against
    max_rounds. Only chat.speech.* counts. Validates the kind_prefix
    filter in count_events."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = _open(chat, m, discord, policy={"max_rounds": 2})
    # If lifecycle counted, the conversation.opened (seq=1) would be 1
    # of 2 already and a single speech would tip the cap. It doesn't.
    _say(chat, m, summary.id, content="speech 1")
    _say(chat, m, summary.id, content="speech 2")
    with pytest.raises(m["ChatConversationStateError"], match="max_rounds=2"):
        _say(chat, m, summary.id, content="speech 3")


# ============================================================================
# 7. Cap binds even on alternating actors
# ============================================================================

def test_max_rounds_caps_globally_not_per_actor(tmp_path, monkeypatch):
    """The cap is op-level. 3 different actors posting once each still
    trip a cap of 2. This is the v3 fix for the 'per-persona cap × N
    personas = 9 events' v2 bug."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = _open(chat, m, discord, policy={"max_rounds": 2})
    _say(chat, m, summary.id, actor="alice", content="1")
    _say(chat, m, summary.id, actor="bob", content="2")
    with pytest.raises(m["ChatConversationStateError"], match="max_rounds=2"):
        _say(chat, m, summary.id, actor="carol", content="3")
