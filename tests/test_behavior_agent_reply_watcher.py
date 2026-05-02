"""RemoteClaudeReplyWatcher — translate PC claude run results into
speech.claim back into the originating v2 op.

Tests use deterministic envelope construction + asyncio for the real
loop. End-to-end PC integration is out of scope; we exercise the
seams: machine event → session task spawn → claude.event 'result'
frame → submit_speech.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import pytest

from conftest import NAS_BRIDGE_ROOT

os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))


def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.agent import RemoteClaudeReplyWatcher
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import ConversationOpenRequest
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.kernel.events import EventEnvelope, EventSummary, encode_event_cursor
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.behaviors.remote_claude.state_service import (
        remote_claude_machine_space_id,
        remote_claude_session_space_id,
    )
    from datetime import datetime, timezone
    db.init_db()
    return locals() | {"db": db}


def _make_envelope(*, m, space_id, kind, content_dict, actor="machine"):
    """Build a broker EventEnvelope with the same shape state_service uses."""
    EventEnvelope = m["EventEnvelope"]
    EventSummary = m["EventSummary"]
    encode = m["encode_event_cursor"]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return EventEnvelope(
        cursor=encode(created_at=now, event_id=str(uuid.uuid4())),
        space_id=space_id,
        event=EventSummary(
            id=str(uuid.uuid4()),
            kind=kind,
            actor_name=actor,
            content=json.dumps(content_dict, ensure_ascii=False),
            created_at=now,
        ),
    )


def _make_op(m, *, opener="@bridge-agent", addressed_to="alice", title="x") -> tuple[str, str]:
    """Open an inquiry op, return (v1_conv_id, v2_op_id)."""
    db = m["db"]
    Thread = m["ChatThreadModel"]
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        discord = t.discord_thread_id
    chat = m["ChatConversationService"]()
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title=title,
            opener_actor=opener.lstrip("@"),
            addressed_to=addressed_to.lstrip("@"),
        ),
    )
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        return summary.id, v1.v2_operation_id


def test_watcher_posts_speech_when_result_frame_arrives(tmp_path, monkeypatch):
    """End-to-end async: dispatch register → simulated machine envelope
    learns session_id → simulated claude.event 'result' frame → speech
    posted to op."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    machine_id = "pc-A"
    v1_id, op_id = _make_op(m, opener="@bridge-agent", addressed_to="alice")

    watcher = m["RemoteClaudeReplyWatcher"](
        broker=broker, chat_service=chat, machine_ids=[machine_id],
    )
    watcher.register_dispatch(
        command_id="cmd-1",
        machine_id=machine_id,
        operation_id=op_id,
        actor_handle="@bridge-agent",
    )

    async def scenario():
        await watcher.start()
        # Allow tasks to subscribe
        await asyncio.sleep(0.05)

        # Publish a machine-level command event with status=running carrying session_id
        machine_env = _make_envelope(
            m=m,
            space_id=m["remote_claude_machine_space_id"](machine_id),
            kind="remote_claude.command.running",
            content_dict={
                "kind": "command",
                "command": {
                    "commandId": "cmd-1",
                    "sessionId": "sess-xyz",
                    "status": "running",
                },
            },
        )
        broker.publish(space_id=machine_env.space_id, item=machine_env)
        # let machine loop dispatch -> session loop subscribe
        await asyncio.sleep(0.1)

        # Publish a claude stream-json 'result' event
        result_env = _make_envelope(
            m=m,
            space_id=m["remote_claude_session_space_id"]("sess-xyz"),
            kind="claude.event",
            content_dict={
                "kind": "claude.event",
                "event": {
                    "type": "result",
                    "subtype": "success",
                    "result": "the build is failing because of a missing migration.",
                },
            },
        )
        broker.publish(space_id=result_env.space_id, item=result_env)
        await asyncio.sleep(0.2)
        await watcher.stop()

    asyncio.run(scenario())

    # Verify the speech was submitted
    from sqlalchemy import select
    from app.behaviors.chat.models import ChatMessageModel
    with db.session_scope() as s:
        msgs = list(s.scalars(
            select(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == v1_id)
            .where(ChatMessageModel.actor_name == "bridge-agent")
        ))
        assert len(msgs) >= 1
        texts = [msg.content for msg in msgs]
        assert any("missing migration" in t for t in texts)


def test_watcher_ignores_unregistered_command(tmp_path, monkeypatch):
    """Machine event for a command we never registered is silently
    ignored (could be from another bridge-agent instance / brain)."""
    m = _bootstrap(tmp_path, monkeypatch)
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    watcher = m["RemoteClaudeReplyWatcher"](
        broker=broker, chat_service=chat, machine_ids=["pc-A"],
    )

    async def scenario():
        await watcher.start()
        await asyncio.sleep(0.05)
        env = _make_envelope(
            m=m,
            space_id=m["remote_claude_machine_space_id"]("pc-A"),
            kind="remote_claude.command.running",
            content_dict={
                "kind": "command",
                "command": {
                    "commandId": "cmd-not-mine",
                    "sessionId": "sess-other",
                    "status": "running",
                },
            },
        )
        broker.publish(space_id=env.space_id, item=env)
        await asyncio.sleep(0.1)
        # No session task should have been created for sess-other
        assert "sess-other" not in watcher._session_tasks
        await watcher.stop()

    asyncio.run(scenario())


def test_watcher_drops_pending_on_claude_exit_without_result(tmp_path, monkeypatch):
    """If run dies (claude.exit) before emitting result frame, watcher
    drops the pending entry quietly (no speech posted)."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    machine_id = "pc-A"
    v1_id, op_id = _make_op(m, title="exit-test")
    watcher = m["RemoteClaudeReplyWatcher"](
        broker=broker, chat_service=chat, machine_ids=[machine_id],
    )
    watcher.register_dispatch(
        command_id="cmd-exit",
        machine_id=machine_id,
        operation_id=op_id,
        actor_handle="@bridge-agent",
    )

    async def scenario():
        await watcher.start()
        await asyncio.sleep(0.05)
        # learn session_id
        broker.publish(
            space_id=m["remote_claude_machine_space_id"](machine_id),
            item=_make_envelope(
                m=m,
                space_id=m["remote_claude_machine_space_id"](machine_id),
                kind="remote_claude.command.running",
                content_dict={
                    "kind": "command",
                    "command": {
                        "commandId": "cmd-exit",
                        "sessionId": "sess-exit",
                        "status": "running",
                    },
                },
            ),
        )
        await asyncio.sleep(0.1)
        # claude.exit before any result
        broker.publish(
            space_id=m["remote_claude_session_space_id"]("sess-exit"),
            item=_make_envelope(
                m=m,
                space_id=m["remote_claude_session_space_id"]("sess-exit"),
                kind="claude.exit",
                content_dict={"kind": "claude.exit", "code": 1},
            ),
        )
        await asyncio.sleep(0.1)
        await watcher.stop()

    asyncio.run(scenario())

    # No bridge-agent message was posted
    from sqlalchemy import select
    from app.behaviors.chat.models import ChatMessageModel
    with db.session_scope() as s:
        msgs = list(s.scalars(
            select(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == v1_id)
            .where(ChatMessageModel.actor_name == "bridge-agent")
            .where(ChatMessageModel.event_kind == "chat.speech.claim")
        ))
        assert msgs == []


def test_watcher_skips_empty_result(tmp_path, monkeypatch):
    """A 'result' frame with empty result text doesn't post an empty
    speech."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    machine_id = "pc-A"
    v1_id, op_id = _make_op(m, title="empty")
    watcher = m["RemoteClaudeReplyWatcher"](
        broker=broker, chat_service=chat, machine_ids=[machine_id],
    )
    watcher.register_dispatch(
        command_id="cmd-empty", machine_id=machine_id,
        operation_id=op_id, actor_handle="@bridge-agent",
    )

    async def scenario():
        await watcher.start()
        await asyncio.sleep(0.05)
        broker.publish(
            space_id=m["remote_claude_machine_space_id"](machine_id),
            item=_make_envelope(
                m=m,
                space_id=m["remote_claude_machine_space_id"](machine_id),
                kind="remote_claude.command.running",
                content_dict={
                    "kind": "command",
                    "command": {
                        "commandId": "cmd-empty",
                        "sessionId": "sess-empty",
                        "status": "running",
                    },
                },
            ),
        )
        await asyncio.sleep(0.1)
        broker.publish(
            space_id=m["remote_claude_session_space_id"]("sess-empty"),
            item=_make_envelope(
                m=m,
                space_id=m["remote_claude_session_space_id"]("sess-empty"),
                kind="claude.event",
                content_dict={
                    "kind": "claude.event",
                    "event": {"type": "result", "subtype": "success", "result": ""},
                },
            ),
        )
        await asyncio.sleep(0.15)
        await watcher.stop()

    asyncio.run(scenario())

    from sqlalchemy import select
    from app.behaviors.chat.models import ChatMessageModel
    with db.session_scope() as s:
        msgs = list(s.scalars(
            select(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == v1_id)
            .where(ChatMessageModel.actor_name == "bridge-agent")
            .where(ChatMessageModel.event_kind == "chat.speech.claim")
        ))
        assert msgs == []
