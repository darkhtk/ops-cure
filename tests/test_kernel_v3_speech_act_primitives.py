"""v3-additive primitives: expected_response + operation policy.

These are storage + plumbing tests. Phase 1 only persists the new
fields and surfaces them on the API + SSE wrapped envelope; phase 2
will enforce them. So the assertions here are 'the field round-trips
through the protocol intact', not 'the kernel rejects bad acts'.
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
    from app.behaviors.chat.models import ChatThreadModel
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


def test_op_policy_persists_with_defaults_when_absent(tmp_path, monkeypatch):
    """Opening an op without a policy still ends up with the default
    governance policy materialized on the v2 op so callers always have
    something concrete to inspect."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry",
            title="t",
            opener_actor="alice",
        ),
    )

    repo = m["V2Repository"]()
    with db.session_scope() as s:
        from app.behaviors.chat.models import ChatConversationModel
        v1 = s.get(ChatConversationModel, summary.id)
        op = repo.get_operation(s, v1.v2_operation_id)
        policy = repo.operation_policy(op)

    assert policy["close_policy"] == m["v2_contract"].CLOSE_POLICY_OPENER_UNILATERAL
    assert policy["join_policy"] == m["v2_contract"].JOIN_POLICY_SELF_OR_INVITE
    assert policy["context_compaction"] == m["v2_contract"].CONTEXT_COMPACTION_NONE
    assert policy["bot_open"] is True


def test_op_policy_round_trips_caller_overrides(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    requested = {
        "close_policy": m["v2_contract"].CLOSE_POLICY_QUORUM,
        "min_ratifiers": 2,
        "max_rounds": 8,
        "bot_open": False,
    }
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry",
            title="t",
            opener_actor="alice",
            policy=requested,
        ),
    )
    repo = m["V2Repository"]()
    with db.session_scope() as s:
        from app.behaviors.chat.models import ChatConversationModel
        v1 = s.get(ChatConversationModel, summary.id)
        op = repo.get_operation(s, v1.v2_operation_id)
        policy = repo.operation_policy(op)

    assert policy["close_policy"] == m["v2_contract"].CLOSE_POLICY_QUORUM
    assert policy["min_ratifiers"] == 2
    assert policy["max_rounds"] == 8
    assert policy["bot_open"] is False


def test_invalid_policy_is_rejected_at_open(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    import pytest
    from app.behaviors.chat.conversation_service import ChatConversationStateError
    with pytest.raises(ChatConversationStateError, match="invalid policy"):
        chat.open_conversation(
            discord_thread_id=discord,
            request=m["ConversationOpenRequest"](
                kind="inquiry", title="t", opener_actor="alice",
                policy={"close_policy": "no_such_policy"},
            ),
        )


def test_expected_response_round_trips_through_event_payload(tmp_path, monkeypatch):
    """expected_response declared on submit_speech persists into the v2
    event's payload._meta.expected_response, and event_expected_response
    pulls it back out cleanly."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
        ),
    )

    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice",
            kind="question",
            content="EU latency cause?",
            addressed_to="investigator",
            expected_response={
                "from_actor_handles": ["@investigator"],
                "kinds": ["answer", "defer"],
                "by_round_seq": 5,
            },
        ),
    )

    repo = m["V2Repository"]()
    from app.behaviors.chat.models import ChatConversationModel
    with db.session_scope() as s:
        v1 = s.get(ChatConversationModel, summary.id)
        events = repo.list_events(s, operation_id=v1.v2_operation_id)
        # find the speech.question
        question = next(e for e in events if e.kind == "chat.speech.question")
        ex = repo.event_expected_response(question)

    assert ex is not None
    assert ex["from_actor_handles"] == ["@investigator"]
    assert ex["kinds"] == ["answer", "defer"]
    assert ex["by_round_seq"] == 5


def test_expected_response_normalizes_handle_at_signs(tmp_path, monkeypatch):
    """validate_expected_response prepends '@' to bare handles so the
    contract is symmetric on the wire (callers don't have to remember)."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="question",
            content="?",
            expected_response={"from_actor_handles": ["investigator", "@reviewer"]},
        ),
    )

    repo = m["V2Repository"]()
    from app.behaviors.chat.models import ChatConversationModel
    with db.session_scope() as s:
        v1 = s.get(ChatConversationModel, summary.id)
        events = repo.list_events(s, operation_id=v1.v2_operation_id)
        question = next(e for e in events if e.kind == "chat.speech.question")
        ex = repo.event_expected_response(question)

    assert ex["from_actor_handles"] == ["@investigator", "@reviewer"]


def test_invalid_expected_response_is_rejected(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
        ),
    )

    import pytest
    from app.behaviors.chat.conversation_service import ChatConversationStateError
    with pytest.raises(ChatConversationStateError, match="invalid expected_response"):
        chat.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                actor_name="alice", kind="question",
                content="?",
                expected_response={"from_actor_handles": ["investigator"], "kinds": ["bogus_kind"]},
            ),
        )


def test_explicit_v2_reply_id_writes_replies_to_before_fanout(tmp_path, monkeypatch):
    """v3-additive replies_to_v2_event_id flow: when an external /v2
    caller provides a reply pointer, it lands on the v2 event row in
    the same transaction as insert (so SSE subscribers see the link
    in real time, not after a re-fetch)."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
        ),
    )
    parent = chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="question",
            content="?",
        ),
    )
    repo = m["V2Repository"]()
    from app.behaviors.chat.models import ChatConversationModel, ChatMessageModel
    with db.session_scope() as s:
        parent_v2_id = s.get(ChatMessageModel, parent.id).v2_event_id
        assert parent_v2_id is not None

    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="bob", kind="answer",
            content="answer",
            replies_to_v2_event_id=parent_v2_id,
        ),
    )

    with db.session_scope() as s:
        v1 = s.get(ChatConversationModel, summary.id)
        events = repo.list_events(s, operation_id=v1.v2_operation_id)
        answer = next(e for e in events if e.kind == "chat.speech.answer")
        assert answer.replies_to_event_id == parent_v2_id
