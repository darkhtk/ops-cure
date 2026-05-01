"""F5: Inbox API + auto-participant for addressed actors."""
from __future__ import annotations

import sys
import uuid

from fastapi.testclient import TestClient

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
    from app.behaviors.chat.models import ChatThreadModel
    from app.main import app
    db.init_db()
    return {
        "db": db, "app": app,
        "Service": ChatConversationService,
        "Open": ConversationOpenRequest,
        "Speech": SpeechActSubmitRequest,
        "Thread": ChatThreadModel,
    }


def _make_thread(db, Thread):
    with db.session_scope() as session:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        session.add(t)
        session.flush()
        return t.discord_thread_id


def test_addressed_actor_appears_in_inbox(tmp_path, monkeypatch):
    """If alice opens an inquiry addressed to claude, claude's inbox
    has that operation. Speaks to F5's promise."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["Service"]()
    discord = _make_thread(db, m["Thread"])
    svc.open_conversation(
        discord_thread_id=discord,
        request=m["Open"](
            kind="inquiry", title="logs?", opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )

    client = TestClient(m["app"])
    resp = client.get(
        "/v2/inbox", params={"actor_handle": "@claude-pca"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["actor_handle"] == "@claude-pca"
    assert len(body["items"]) == 1
    assert body["items"][0]["kind"] == "inquiry"
    assert body["items"][0]["role"] == "addressed"
    assert body["items"][0]["state"] == "open"


def test_late_addressed_in_speech_becomes_participant(tmp_path, monkeypatch):
    """A speech act addressed to bob mid-conversation upgrades bob to
    a participant on that operation."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["Service"]()
    discord = _make_thread(db, m["Thread"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["Open"](
            kind="proposal", title="adopt structured logging",
            opener_actor="alice",
        ),
    )
    # bob was not in the open. Now alice addresses him directly.
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["Speech"](
            kind="claim", actor_name="alice",
            content="bob -- thoughts?", addressed_to="bob",
        ),
    )

    client = TestClient(m["app"])
    resp = client.get(
        "/v2/inbox", params={"actor_handle": "@bob"},
        headers={"Authorization": "Bearer t"},
    )
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["role"] == "addressed"


def test_inbox_filter_by_state_and_role(tmp_path, monkeypatch):
    """state= and roles= query params filter the result."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["Service"]()
    discord = _make_thread(db, m["Thread"])

    # alice opens 2 ops, closes one
    op1 = svc.open_conversation(
        discord_thread_id=discord,
        request=m["Open"](
            kind="inquiry", title="q1", opener_actor="alice",
        ),
    )
    svc.open_conversation(
        discord_thread_id=discord,
        request=m["Open"](
            kind="proposal", title="p1", opener_actor="alice",
        ),
    )
    svc.close_conversation(
        conversation_id=op1.id, closed_by="alice",
        resolution="answered", summary="seen",
    )

    client = TestClient(m["app"])
    # state=open should yield only the one open op
    resp = client.get(
        "/v2/inbox", params={"actor_handle": "@alice", "state": "open"},
        headers={"Authorization": "Bearer t"},
    )
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["state"] == "open"

    # role=opener should still match both
    resp2 = client.get(
        "/v2/inbox", params={"actor_handle": "@alice", "roles": "opener"},
        headers={"Authorization": "Bearer t"},
    )
    assert len(resp2.json()["items"]) == 2


def test_inbox_unknown_actor_returns_empty(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    client = TestClient(m["app"])
    resp = client.get(
        "/v2/inbox", params={"actor_handle": "@ghost"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"actor_handle": "@ghost", "items": []}


def test_inbox_requires_auth(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    client = TestClient(m["app"])
    resp = client.get("/v2/inbox", params={"actor_handle": "@x"})
    assert resp.status_code == 401
