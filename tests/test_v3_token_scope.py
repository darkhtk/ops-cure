"""v3 phase 4 — per-actor token scopes (admin / speak / read-only)."""
from __future__ import annotations

import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setenv("BRIDGE_POLICY_SWEEPER_SECONDS", "0")
    monkeypatch.delenv("BRIDGE_REQUIRE_ACTOR_TOKEN", raising=False)
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    from app.main import app as fastapi_app
    import app.db as db
    db.init_db()
    from app.behaviors.chat.models import ChatThreadModel
    with db.session_scope() as s:
        t = ChatThreadModel(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="scope-thread", title="t", created_by="alice",
        )
        s.add(t)
    with TestClient(fastapi_app) as c:
        c.headers.update({"Authorization": "Bearer t"})
        yield c


def _issue(client, *, handle, scope):
    r = client.post(
        f"/v2/actors/{handle}/tokens",
        json={"scope": scope},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_unknown_scope_rejected_at_issue(client):
    r = client.post(
        "/v2/actors/alice/tokens",
        json={"scope": "god-mode"},
    )
    assert r.status_code == 400
    assert "scope" in r.text.lower()


def test_default_scope_is_admin(client):
    r = client.post("/v2/actors/alice/tokens", json={})
    assert r.status_code == 201
    assert r.json()["scope"] == "admin"


def test_speak_token_can_post_events(client):
    issued = _issue(client, handle="alice", scope="speak")
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": issued["token"]},
        json={
            "space_id": "scope-thread", "kind": "inquiry",
            "title": "speak test", "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 201, r.text


def test_read_only_token_blocked_from_posting_events(client):
    issued = _issue(client, handle="alice", scope="read-only")
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": issued["token"]},
        json={
            "space_id": "scope-thread", "kind": "inquiry",
            "title": "read-only test", "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 403, r.text
    assert "scope" in r.text.lower()


def test_read_only_token_blocked_on_speech_post(client):
    """Even a fully-authorized speak action (post a chat.speech event)
    fails when the bearing token is read-only scope."""
    speak_issued = _issue(client, handle="alice", scope="speak")
    # alice opens an op with her speak token
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": speak_issued["token"]},
        json={
            "space_id": "scope-thread", "kind": "inquiry",
            "title": "for read-only test", "opener_actor_handle": "@alice",
        },
    )
    op_id = r.json()["id"]
    # Now alice rotates to a read-only token and tries to speak
    ro_issued = _issue(client, handle="alice", scope="read-only")
    r = client.post(
        f"/v2/operations/{op_id}/events",
        headers={"X-Actor-Token": ro_issued["token"]},
        json={
            "actor_handle": "@alice", "kind": "speech.claim",
            "payload": {"text": "should not land"},
        },
    )
    assert r.status_code == 403


def test_admin_token_allowed_everywhere(client):
    issued = _issue(client, handle="alice", scope="admin")
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": issued["token"]},
        json={
            "space_id": "scope-thread", "kind": "inquiry",
            "title": "admin test", "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 201
