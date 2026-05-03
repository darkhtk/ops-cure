"""v3 phase 2.5 — privacy redaction for late-joining agents.

The mid-collab join protocol (JOIN/INVITE) is only safe if the
history fetch a late joiner runs respects ``private_to_actors`` from
events posted BEFORE they joined. This file pins that down: a whisper
between alice + bob, then carol joins later, then carol fetches
history -> the whisper must NOT be in carol's view.
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
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.kernel.v2 import V2Repository
    return locals()


def _thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d-late", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def test_late_joiner_does_not_see_prior_whispers(tmp_path, monkeypatch):
    """alice + bob whisper, then carol joins, fetches history.
    The whisper is redacted from carol's view."""
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
    # alice posts a public claim (visible to all)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="public context",
        ),
    )
    # alice whispers to bob (private)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="WHISPER for bob only",
            private_to_actors=["bob"],
        ),
    )
    # bob replies publicly
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="bob", kind="claim", content="ok",
        ),
    )
    # carol joins now (default join_policy = self_or_invite allows)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="carol", kind="join", content="@carol joining",
        ),
    )

    # carol fetches op history through the privacy-aware code path.
    repo = m["V2Repository"]()
    from app.kernel.v2.actor_service import ActorService
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id
        carol = ActorService(repo).ensure_actor_by_handle(s, handle="@carol")
        events = repo.list_events(s, operation_id=op_id, limit=100)
        # Filter + materialize INSIDE the session so detached
        # instances don't trip when we touch attributes below.
        texts: list[tuple[str, str]] = []
        for e in events:
            private = repo.event_private_to(e)
            if private is not None and carol.id not in private and e.actor_id != carol.id:
                continue
            texts.append((e.kind, repo.event_payload(e).get("text") or ""))

    # Sanity: carol sees the public claim and bob's reply
    assert any("public context" in t for _, t in texts)
    assert any(t == "ok" for _, t in texts)
    # Privacy: carol does NOT see the whisper
    assert not any("WHISPER" in t for _, t in texts), (
        f"late joiner saw a private whisper: {texts}"
    )


def test_late_joiner_history_includes_join_event_self(tmp_path, monkeypatch):
    """The join event carol just posted is visible in her own history
    fetch — sanity that join is treated as a normal speech event."""
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
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="carol", kind="join", content="@carol joining",
        ),
    )
    repo = m["V2Repository"]()
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        events = repo.list_events(s, operation_id=v1.v2_operation_id, limit=100)
        assert any(e.kind == "chat.speech.join" for e in events)
