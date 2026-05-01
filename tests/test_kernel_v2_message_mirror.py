"""F4: every ChatMessage insert mirrors to a v2 OperationEvent."""
from __future__ import annotations

import sys
import uuid

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
    from app.kernel.v2 import V2Repository
    db.init_db()
    return {
        "db": db,
        "Service": ChatConversationService,
        "OpenRequest": ConversationOpenRequest,
        "SpeechRequest": SpeechActSubmitRequest,
        "Thread": ChatThreadModel,
        "Conv": ChatConversationModel,
        "Repo": V2Repository,
    }


def _make_thread(db, Thread):
    with db.session_scope() as session:
        thread = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        session.add(thread)
        session.flush()
        return thread.discord_thread_id


def test_open_conversation_emits_opened_event_in_v2(tmp_path, monkeypatch):
    """The chat.conversation.opened ChatMessageModel should land as a
    v2 OperationEvent on the same operation."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["Service"]()
    discord_id = _make_thread(db, m["Thread"])

    summary = svc.open_conversation(
        discord_thread_id=discord_id,
        request=m["OpenRequest"](
            kind="inquiry", title="q", opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )

    repo = m["Repo"]()
    with db.session_scope() as session:
        v1 = session.get(m["Conv"], summary.id)
        events = repo.list_events(session, operation_id=v1.v2_operation_id)
        kinds = [e.kind for e in events]
        # there should be at least the conversation.opened event
        assert "chat.conversation.opened" in kinds


def test_speech_act_mirrors_with_addressed_actor_id(tmp_path, monkeypatch):
    """A speech act addressed to bob lands in v2 with addressed_to_actor_ids
    referencing bob's actor row."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["Service"]()
    discord_id = _make_thread(db, m["Thread"])

    summary = svc.open_conversation(
        discord_thread_id=discord_id,
        request=m["OpenRequest"](
            kind="inquiry", title="q", opener_actor="alice",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechRequest"](
            kind="claim", actor_name="alice", content="bob look at this",
            addressed_to="bob",
        ),
    )

    repo = m["Repo"]()
    with db.session_scope() as session:
        v1 = session.get(m["Conv"], summary.id)
        events = repo.list_events(session, operation_id=v1.v2_operation_id)
        speech_events = [e for e in events if e.kind == "chat.speech.claim"]
        assert len(speech_events) == 1
        addr = repo.event_addressed_to(speech_events[0])
        assert len(addr) == 1
        bob = repo.get_actor_by_handle(session, "@bob")
        assert addr[0] == bob.id


def test_close_emits_closed_event_in_v2(tmp_path, monkeypatch):
    """Close path also mirrors the chat.conversation.closed event."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["Service"]()
    discord_id = _make_thread(db, m["Thread"])
    summary = svc.open_conversation(
        discord_thread_id=discord_id,
        request=m["OpenRequest"](
            kind="proposal", title="p", opener_actor="alice",
        ),
    )
    svc.close_conversation(
        conversation_id=summary.id, closed_by="alice",
        resolution="accepted", summary="ok",
    )
    repo = m["Repo"]()
    with db.session_scope() as session:
        v1 = session.get(m["Conv"], summary.id)
        events = repo.list_events(session, operation_id=v1.v2_operation_id)
        kinds = [e.kind for e in events]
        assert "chat.conversation.closed" in kinds
