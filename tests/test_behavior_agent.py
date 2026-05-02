"""Agent runner dispatch round-trip via EchoBrain (in-process).

Tests cover the deterministic seam (sync ``dispatch``) so an asyncio
loop isn't required. AgentRunner is now used only by tests +
protocol_test scenarios; production agents are external clients of
/v2/inbox/stream and don't go through this path.
"""
from __future__ import annotations

import json
import sys
import uuid

import pytest

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.behaviors.agent import (
        AgentRunner, EchoBrain, ActionResult,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.kernel.v2 import V2Repository
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread, suffix="1"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def test_runner_replies_to_addressed_question_via_echo_brain(tmp_path, monkeypatch):
    """alice 가 @claude-pca 에게 question -> broker fan-out 으로 envelope 도착
    -> dispatch 가 EchoBrain 의 reply 를 chat_service 통해 발화 -> v2 op 에
    speech.claim 1개 추가."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="logs?",
            opener_actor="alice", addressed_to="claude-pca",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice",
            content="where are last week's logs?",
            addressed_to="claude-pca",
        ),
    )

    runner = m["AgentRunner"](
        actor_handle="@claude-pca",
        brain=m["EchoBrain"](),
        broker=broker,
        chat_service=chat,
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id
        claude_id = repo.get_actor_by_handle(session, "@claude-pca").id

    backlog = list(broker._backlog.get(f"v2:inbox:{claude_id}", []))
    # The question envelope is the most recent. Find it.
    question_envelope = None
    for env in reversed(backlog):
        if env.event.kind == "chat.speech.question":
            question_envelope = env
            break
    assert question_envelope is not None

    results = runner.dispatch(question_envelope)
    assert len(results) == 1
    assert results[0].action == "speech.claim"
    assert results[0].delivered

    # v2 op should now have an additional speech.claim event
    with db.session_scope() as session:
        events = repo.list_events(session, operation_id=op_id, limit=100)
        claim_texts = [
            repo.event_payload(e).get("text", "")
            for e in events if e.kind == "chat.speech.claim"
        ]
        assert any(t.startswith("echo:") for t in claim_texts)


def test_runner_ignores_self_authored_envelopes(tmp_path, monkeypatch):
    """Loop prevention: the runner sees its own broker fanout (it's a
    participant) but must not respond to it."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"], suffix="self")
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x",
            opener_actor="claude-pca", addressed_to="alice",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="claude-pca", content="hi alice",
            addressed_to="alice",
        ),
    )

    runner = m["AgentRunner"](
        actor_handle="@claude-pca",
        brain=m["EchoBrain"](),
        broker=broker,
        chat_service=chat,
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        claude_id = repo.get_actor_by_handle(session, "@claude-pca").id
    backlog = list(broker._backlog.get(f"v2:inbox:{claude_id}", []))
    # Find the question envelope authored by claude-pca.
    self_authored = [
        e for e in backlog
        if e.event.kind == "chat.speech.question" and e.event.actor_name == claude_id
    ]
    assert self_authored, "claude-pca's own question should be in their backlog"
    results = runner.dispatch(self_authored[0])
    assert results == []  # ignored


def test_runner_ignores_event_addressed_to_other_actor(tmp_path, monkeypatch):
    """A question addressed to alice should not provoke a response from
    @claude-pca even though they're a participant."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"], suffix="other")
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x", opener_actor="operator",
            addressed_to="claude-pca",
        ),
    )
    # Now operator addresses alice (not pca)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="operator", content="alice please",
            addressed_to="alice",
        ),
    )
    runner = m["AgentRunner"](
        actor_handle="@claude-pca",
        brain=m["EchoBrain"](),
        broker=broker,
        chat_service=chat,
    )
    repo = m["V2Repository"]()
    with db.session_scope() as session:
        claude_id = repo.get_actor_by_handle(session, "@claude-pca").id
    backlog = list(broker._backlog.get(f"v2:inbox:{claude_id}", []))
    # Find the question addressed to alice
    target = [
        e for e in backlog
        if e.event.kind == "chat.speech.question"
        and "alice please" in e.event.content
    ]
    assert target
    results = runner.dispatch(target[0])
    assert results == []


def test_runner_honors_whisper_redaction_in_context(tmp_path, monkeypatch):
    """If a whisper exists between alice and operator, claude-pca's
    context (recent_events) should NOT include it -- defense in depth
    even though broker shouldn't have published it to claude in the
    first place."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"], suffix="whisper")
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x",
            opener_actor="alice", addressed_to="claude-pca",
        ),
    )
    # whisper from alice to operator (NOT to claude-pca)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="psst operator only",
            private_to_actors=["operator"],
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice",
            content="public question for claude-pca",
            addressed_to="claude-pca",
        ),
    )

    runner = m["AgentRunner"](
        actor_handle="@claude-pca",
        brain=m["EchoBrain"](),
        broker=broker,
        chat_service=chat,
    )
    repo = m["V2Repository"]()
    with db.session_scope() as session:
        claude_id = repo.get_actor_by_handle(session, "@claude-pca").id
    backlog = list(broker._backlog.get(f"v2:inbox:{claude_id}", []))
    question_env = next(
        e for e in backlog
        if e.event.kind == "chat.speech.question" and "claude-pca" in e.event.content or
        "public question" in e.event.content
    )
    # Probe context build directly (not via dispatch which would also
    # respond -- here we just want to inspect what the brain WOULD see).
    wrapped = json.loads(question_env.event.content)
    context = runner._build_context(wrapped["operation_id"], question_env)
    texts_in_context = [
        e["payload"].get("text", "") for e in context["recent_events"]
    ]
    assert "public question for claude-pca" in texts_in_context
    assert "psst operator only" not in texts_in_context  # whisper hidden


def test_action_with_unknown_kind_records_failure(tmp_path, monkeypatch):
    """Brain returning an unknown action type doesn't crash; the runner
    records a failure ActionResult so observability is preserved."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"], suffix="bad")
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x",
            opener_actor="alice", addressed_to="claude-pca",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice", content="?",
            addressed_to="claude-pca",
        ),
    )

    class WeirdBrain:
        def respond(self, payload, context):
            return [{"action": "do_the_dance", "text": "wrong"}]

    runner = m["AgentRunner"](
        actor_handle="@claude-pca",
        brain=WeirdBrain(),
        broker=broker,
        chat_service=chat,
    )
    repo = m["V2Repository"]()
    with db.session_scope() as session:
        claude_id = repo.get_actor_by_handle(session, "@claude-pca").id
    backlog = list(broker._backlog.get(f"v2:inbox:{claude_id}", []))
    question_env = next(e for e in backlog if e.event.kind == "chat.speech.question")
    results = runner.dispatch(question_env)
    assert len(results) == 1
    assert results[0].action == "do_the_dance"
    assert not results[0].delivered
    assert results[0].detail == "unknown action kind"


# build_default_agent_service was removed when in-process agent hosting
# was retired. Agents now run as external clients of /v2/inbox/stream.
# AgentRunner + brains remain available for unit + protocol_test usage.
