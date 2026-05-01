"""F7: v2 operation/event/artifact reader API + privacy redaction."""
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
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.main import app
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}


def test_get_operation_returns_v1_mirrored_summary(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="logs?", opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id

    client = TestClient(m["app"])
    resp = client.get(f"/v2/operations/{op_id}", headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == op_id
    assert body["kind"] == "inquiry"
    assert body["title"] == "logs?"
    assert body["state"] == "open"
    roles = sorted(p["role"] for p in body["participants"])
    assert roles == ["addressed", "opener"]


def test_get_events_orders_by_seq_and_filters_kind(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="proposal", title="p", opener_actor="alice",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="first",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice", content="why?",
        ),
    )
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id

    client = TestClient(m["app"])
    resp = client.get(f"/v2/operations/{op_id}/events", headers=_AUTH)
    body = resp.json()
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == sorted(seqs)
    kinds = [e["kind"] for e in body["events"]]
    assert "chat.conversation.opened" in kinds
    assert "chat.speech.claim" in kinds
    assert "chat.speech.question" in kinds

    # filter
    resp2 = client.get(
        f"/v2/operations/{op_id}/events",
        params={"kinds": "chat.speech.claim,chat.speech.question"},
        headers=_AUTH,
    )
    body2 = resp2.json()
    assert {e["kind"] for e in body2["events"]} == {
        "chat.speech.claim", "chat.speech.question",
    }


def test_whisper_redacted_for_non_recipient(tmp_path, monkeypatch):
    """Alice whispers to bob. Carol's view of the events is missing
    the whisper (counted in redacted_count). Bob's view shows it."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="proposal", title="risky thing", opener_actor="alice",
        ),
    )
    # ensure carol exists in actors_v2 (open with addressed_to)
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="bob, between us",
            private_to_actors=["bob"],
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="hi carol",
            addressed_to="carol",
        ),
    )
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id

    client = TestClient(m["app"])
    # carol's view: whisper is hidden
    r_carol = client.get(
        f"/v2/operations/{op_id}/events",
        params={"actor_handle": "@carol"}, headers=_AUTH,
    )
    body_carol = r_carol.json()
    assert body_carol["redacted_count"] == 1
    contents = [e["payload"].get("text", "") for e in body_carol["events"]]
    assert "bob, between us" not in contents

    # bob's view: whisper is present
    r_bob = client.get(
        f"/v2/operations/{op_id}/events",
        params={"actor_handle": "@bob"}, headers=_AUTH,
    )
    body_bob = r_bob.json()
    assert body_bob["redacted_count"] == 0
    contents_bob = [e["payload"].get("text", "") for e in body_bob["events"]]
    assert "bob, between us" in contents_bob


def test_mark_seen_advances_cursor_and_unread_drops(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="q", opener_actor="alice",
            addressed_to="bob",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="ping bob",
        ),
    )
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id

    client = TestClient(m["app"])
    # bob's unread count > 0 before seen
    pre = client.get(
        "/v2/inbox/unread-count", params={"actor_handle": "@bob"}, headers=_AUTH,
    ).json()
    assert pre["unread_total"] >= 1

    # find latest seq
    events = client.get(
        f"/v2/operations/{op_id}/events", headers=_AUTH,
    ).json()["events"]
    last_seq = events[-1]["seq"]

    r = client.post(
        f"/v2/operations/{op_id}/seen",
        params={"actor_handle": "@bob", "seq": last_seq},
        headers=_AUTH,
    )
    assert r.status_code == 200
    post = client.get(
        "/v2/inbox/unread-count", params={"actor_handle": "@bob"}, headers=_AUTH,
    ).json()
    assert post["unread_total"] == 0


def test_404_for_unknown_operation(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    client = TestClient(m["app"])
    r = client.get("/v2/operations/no-such-op", headers=_AUTH)
    assert r.status_code == 404
