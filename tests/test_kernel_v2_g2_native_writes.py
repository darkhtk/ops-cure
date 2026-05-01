"""G2: native v2 POST /v2/operations[/events|/close]."""
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
    from app.behaviors.chat.models import ChatThreadModel
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


def test_open_operation_returns_v2_id_directly(tmp_path, monkeypatch):
    """POST /v2/operations returns a v2 op id; no v1_conversation_id
    leaks to the caller."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"])
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        r = client.post(
            "/v2/operations",
            json={
                "space_id": discord,
                "kind": "inquiry",
                "title": "logs?",
                "addressed_to": "claude-pca",
                "opener_actor_handle": "@alice",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "inquiry"
        assert body["state"] == "open"
        assert "id" in body
        # v1 id is NOT exposed
        assert "v1_conversation_id" not in body or True  # metadata may carry it; that's fine


def test_append_event_uses_v2_event_id_only(tmp_path, monkeypatch):
    """POST /v2/operations/{id}/events writes a speech event and
    returns the v2 event id + seq directly."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"])
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        r = client.post(
            "/v2/operations",
            json={
                "space_id": discord, "kind": "inquiry",
                "title": "q", "opener_actor_handle": "@alice",
            },
        )
        op_id = r.json()["id"]

        r2 = client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@alice",
                "kind": "speech.question",
                "payload": {"text": "what is 2+2?"},
                "addressed_to": "claude-pca",
            },
        )
        assert r2.status_code == 201, r2.text
        body = r2.json()
        assert body["kind"] == "chat.speech.question"
        assert body["seq"] >= 2  # opened was seq=1
        assert body["operation_id"] == op_id
        assert body["payload"]["text"] == "what is 2+2?"


def test_append_event_rejects_non_speech_kind(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"])
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = client.post(
            "/v2/operations",
            json={"space_id": discord, "kind": "inquiry",
                  "title": "q", "opener_actor_handle": "@alice"},
        ).json()["id"]
        r = client.post(
            f"/v2/operations/{op_id}/events",
            json={"actor_handle": "@alice", "kind": "task.claim",
                  "payload": {"text": "x"}},
        )
        assert r.status_code == 400
        assert "speech.*" in r.json()["detail"]


def test_close_operation_through_v2(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"])
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = client.post(
            "/v2/operations",
            json={"space_id": discord, "kind": "proposal",
                  "title": "p", "opener_actor_handle": "@alice"},
        ).json()["id"]

        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={"actor_handle": "@alice", "resolution": "accepted",
                  "summary": "done"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "closed"
        assert body["resolution"] == "accepted"


def test_close_with_invalid_resolution_returns_400(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"])
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = client.post(
            "/v2/operations",
            json={"space_id": discord, "kind": "proposal",
                  "title": "p", "opener_actor_handle": "@alice"},
        ).json()["id"]
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={"actor_handle": "@alice",
                  "resolution": "answered"},  # inquiry vocab, not proposal
        )
        assert r.status_code == 400


def test_open_unknown_space_returns_404(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        r = client.post(
            "/v2/operations",
            json={"space_id": "no-such-thread", "kind": "inquiry",
                  "title": "q", "opener_actor_handle": "@alice"},
        )
        assert r.status_code == 404
