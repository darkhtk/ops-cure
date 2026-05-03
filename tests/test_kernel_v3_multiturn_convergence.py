"""v3 phase 2 — multi-turn collab convergence.

The persona / smoke runs each ran one round. This test exercises a
~10-round op where:
  - claim → object → answer → propose → ratify (alice) → ratify (carol)
  - quorum (min_ratifiers=2) is satisfied by alice + carol
  - close transitions cleanly
  - all events survive in correct order with reply-chain links

This is the longest-horizon scenario v3 currently supports without
``rolling_summary`` or ``by_round_seq`` enforcement (Phase 2.5).
"""
from __future__ import annotations

import sys
import uuid

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
    from app.behaviors.chat.conversation_service import ChatConversationService
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


def _thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d-multiturn", title="t-multiturn", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def test_full_disagreement_then_quorum_close_runs_to_completion(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])

    # Open with quorum=2 close policy.
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="proposal", title="key rotation Q3",
            opener_actor="alice",
            policy={
                "close_policy": m["v2_contract"].CLOSE_POLICY_QUORUM,
                "min_ratifiers": 2,
            },
        ),
    )

    repo = m["V2Repository"]()

    def post(actor, kind, content, *, replies_to=None, expected_response=None):
        s = chat.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                actor_name=actor, kind=kind, content=content,
                replies_to_v2_event_id=replies_to,
                expected_response=expected_response,
            ),
        )
        # D9 / rev-9: stamp close-intent on ratify events so the
        # quorum gate counts them.
        if kind == "ratify":
            import json as _json
            from app.behaviors.chat.models import ChatMessageModel
            from app.kernel.v2.models import OperationEventV2Model
            with db.session_scope() as _db:
                v1_msg = _db.get(ChatMessageModel, s.id)
                if v1_msg is not None and v1_msg.v2_event_id:
                    v2_ev = _db.get(OperationEventV2Model, v1_msg.v2_event_id)
                    if v2_ev is not None:
                        try:
                            ex = _json.loads(v2_ev.payload_json or "{}")
                        except Exception:
                            ex = {}
                        if not isinstance(ex, dict):
                            ex = {}
                        ex["intent"] = "close"
                        v2_ev.payload_json = _json.dumps(ex, ensure_ascii=False)
                        _db.flush()
        return s

    def v2id(msg):
        with db.session_scope() as s:
            return s.get(m["ChatMessageModel"], msg.id).v2_event_id

    # Round 1: alice asks a question with a reply contract.
    q = post(
        "alice", "question",
        "Should we rotate backup encryption keys this quarter?",
        expected_response={
            "from_actor_handles": ["@bob", "@carol"],
            "kinds": ["object", "answer", "propose"],
        },
    )
    q_id = v2id(q)

    # Round 2: bob objects (allowed kind).
    obj = post(
        "bob", "object",
        "Rotation this quarter is risky; we just did infra changes.",
        replies_to=q_id,
    )
    obj_id = v2id(obj)

    # Round 3: carol answers.
    ans = post(
        "carol", "answer",
        "Compliance window closes Aug 31; we're already past safe.",
        replies_to=q_id,
    )
    ans_id = v2id(ans)

    # Round 4: alice asks a follow-up addressed to bob with a tighter
    # whitelist (must be agree / object / propose).
    q2 = post(
        "alice", "question",
        "@bob — can you propose a safer rotation window?",
        replies_to=obj_id,
        expected_response={
            "from_actor_handles": ["@bob"],
            "kinds": ["agree", "object", "propose"],
        },
    )
    q2_id = v2id(q2)

    # Round 5: bob proposes.
    prop = post(
        "bob", "propose",
        "Stage in two windows: Aug 12 dry-run + Aug 19 live.",
        replies_to=q2_id,
    )
    prop_id = v2id(prop)

    # Round 6: carol agrees.
    post("carol", "agree", "Stage approach SGTM.", replies_to=prop_id)

    # Round 7: bob ratifies the close.
    post("bob", "ratify", "ratify close")
    # Round 8: carol ratifies the close.
    post("carol", "ratify", "ratify close")

    # Quorum reached -> close passes.
    closed = chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="accepted",
        summary="staged rotation Aug 12 + Aug 19",
    )
    assert closed.state == "closed"
    assert closed.resolution == "accepted"

    # Verify event log integrity.
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        events = repo.list_events(s, operation_id=v1.v2_operation_id, limit=200)
        kinds = [e.kind for e in events]
        # 1 opened + 1 question + 1 object + 1 answer + 1 question +
        # 1 propose + 1 agree + 2 ratify + 1 closed = 10 events
        assert "chat.conversation.opened" in kinds
        assert "chat.conversation.closed" in kinds
        assert kinds.count("chat.speech.ratify") == 2
        assert kinds.count("chat.speech.object") == 1
        assert kinds.count("chat.speech.propose") == 1
        # Reply chain links survive
        for e in events:
            if e.kind == "chat.speech.object":
                assert e.replies_to_event_id == q_id
            if e.kind == "chat.speech.propose":
                assert e.replies_to_event_id == q2_id
        # Events are seq-ordered
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs)


def test_premature_close_under_quorum_blocked_then_unblocked(tmp_path, monkeypatch):
    """Drive an op through a few exchanges, attempt close before
    quorum, observe rejection, complete the second ratify, observe
    success. Validates that quorum is checked at every close attempt,
    not cached."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t",
            opener_actor="alice",
            policy={
                "close_policy": m["v2_contract"].CLOSE_POLICY_QUORUM,
                "min_ratifiers": 2,
            },
        ),
    )

    def post(actor, kind, content):
        s = chat.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                actor_name=actor, kind=kind, content=content,
            ),
        )
        # D9 / rev-9: ratifies count toward quorum only when close-
        # intent. Tests in this file use ratify exclusively for
        # quorum voting, so stamp intent=close automatically.
        if kind == "ratify":
            import json as _json
            from app.behaviors.chat.models import ChatMessageModel
            from app.kernel.v2.models import OperationEventV2Model
            with m["db"].session_scope() as _db:
                v1_msg = _db.get(ChatMessageModel, s.id)
                if v1_msg is not None and v1_msg.v2_event_id:
                    v2_ev = _db.get(OperationEventV2Model, v1_msg.v2_event_id)
                    if v2_ev is not None:
                        try:
                            ex = _json.loads(v2_ev.payload_json or "{}")
                        except Exception:
                            ex = {}
                        if not isinstance(ex, dict):
                            ex = {}
                        ex["intent"] = "close"
                        v2_ev.payload_json = _json.dumps(ex, ensure_ascii=False)
                        _db.flush()
        return s

    post("alice", "question", "Should we rotate?")
    post("bob", "answer", "Yes — compliance window")
    post("bob", "ratify", "first ratify")

    import pytest
    from app.behaviors.chat.conversation_service import ChatConversationStateError
    # 1 ratify, quorum=2 → blocked
    with pytest.raises(ChatConversationStateError, match="quorum"):
        chat.close_conversation(
            conversation_id=summary.id, closed_by="alice", resolution="answered",
        )
    # second ratifier
    post("carol", "ratify", "second ratify")
    closed = chat.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="answered",
    )
    assert closed.state == "closed"
