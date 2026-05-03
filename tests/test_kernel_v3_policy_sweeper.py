"""v3 phase 2.5 — by_round_seq auto-DEFER sweeper."""
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
    from app.kernel.v2 import V2Repository, PolicySweeper
    from app.kernel.v2 import contract as v2_contract
    return locals()


def _thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d-sweeper", title="t-sweeper", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def test_sweeper_emits_defer_when_by_round_elapses(tmp_path, monkeypatch):
    """When the addressed actor never replies and op MAX(seq) exceeds
    expected_response.by_round_seq, the sweeper emits speech.defer
    on their behalf."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            addressed_to="bob",
        ),
    )
    # Trigger: alice asks, by_round_seq=3 (must answer before seq 3+).
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="question", content="cause?",
            addressed_to="bob",
            expected_response={
                "from_actor_handles": ["@bob"],
                "kinds": ["answer"],
                "by_round_seq": 3,
            },
        ),
    )
    # Bob never answers. Push the seq forward via alice posting more.
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="follow up 1",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="follow up 2",
        ),
    )
    # MAX(seq) is now 4 (1 opened + 3 speeches), past by_round_seq=3.

    # Run the sweeper once.
    sweeper = m["PolicySweeper"](
        chat_service=chat,
        session_scope=db.session_scope,
        repo=m["V2Repository"](),
    )
    emitted = sweeper._sweep_once()
    assert emitted == 1, f"expected 1 defer emission, got {emitted}"

    # Verify a speech.defer from bob exists in the op.
    repo = m["V2Repository"]()
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        events = repo.list_events(s, operation_id=v1.v2_operation_id, limit=200)
        defers = [e for e in events if e.kind == "chat.speech.defer"]
        assert len(defers) == 1
        defer = defers[0]
        # Defer payload should mention auto + bob's handle
        payload = repo.event_payload(defer)
        assert "auto-defer" in (payload.get("text") or "")
        assert "@bob" in (payload.get("text") or "")
        # Defer's actor must be bob
        from app.kernel.v2.models import ActorV2Model
        bob = s.get(ActorV2Model, defer.actor_id)
        assert bob.handle == "@bob"
        # Reply chain links the defer to the trigger
        triggers = [e for e in events if e.kind == "chat.speech.question"]
        assert defer.replies_to_event_id == triggers[0].id


def test_sweeper_idempotent_does_not_double_emit(tmp_path, monkeypatch):
    """A second sweep over the same op does not emit a duplicate
    defer — the existing speech.defer with replies_to_event_id ==
    trigger.id is detected."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            addressed_to="bob",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="question", content="?",
            addressed_to="bob",
            expected_response={
                "from_actor_handles": ["@bob"], "by_round_seq": 2,
            },
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="bump",
        ),
    )
    sweeper = m["PolicySweeper"](
        chat_service=chat,
        session_scope=db.session_scope,
        repo=m["V2Repository"](),
    )
    first = sweeper._sweep_once()
    second = sweeper._sweep_once()
    assert first == 1
    assert second == 0  # idempotent


def test_sweeper_skips_when_addressee_already_replied(tmp_path, monkeypatch):
    """If the addressee replied (with replies_to_event_id pointing at
    the trigger) before the sweep, no defer is emitted."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            addressed_to="bob",
        ),
    )
    trigger = chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="question", content="?",
            addressed_to="bob",
            expected_response={
                "from_actor_handles": ["@bob"], "by_round_seq": 2,
            },
        ),
    )
    # Resolve trigger v2 id
    with db.session_scope() as s:
        trigger_v2 = s.get(m["ChatMessageModel"], trigger.id).v2_event_id
    # Bob answers with replies_to set
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="bob", kind="answer", content="yes",
            replies_to_v2_event_id=trigger_v2,
        ),
    )
    # Push seq past by_round_seq
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="bump",
        ),
    )

    sweeper = m["PolicySweeper"](
        chat_service=chat,
        session_scope=db.session_scope,
        repo=m["V2Repository"](),
    )
    assert sweeper._sweep_once() == 0


def test_sweeper_no_op_when_window_still_open(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"](
        subscription_broker=m["InProcessSubscriptionBroker"](),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
            addressed_to="bob",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="question", content="?",
            addressed_to="bob",
            expected_response={
                "from_actor_handles": ["@bob"],
                "by_round_seq": 999,  # nowhere near
            },
        ),
    )
    sweeper = m["PolicySweeper"](
        chat_service=chat,
        session_scope=db.session_scope,
        repo=m["V2Repository"](),
    )
    assert sweeper._sweep_once() == 0
