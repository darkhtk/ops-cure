"""v3 phase 3.x — per-actor token issuance + handle binding."""
from __future__ import annotations

import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "shared-admin")
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
    # Provision a thread for op tests
    from app.behaviors.chat.models import ChatThreadModel
    with db.session_scope() as s:
        t = ChatThreadModel(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="actor-token-thread", title="t", created_by="alice",
        )
        s.add(t)
    with TestClient(fastapi_app) as c:
        c.headers.update({"Authorization": "Bearer shared-admin"})
        yield c


def test_issue_token_returns_plaintext_once_then_lists_metadata(client):
    """POST returns the plaintext token; GET only returns metadata
    (no plaintext). Plaintext is unrecoverable after issue."""
    r = client.post(
        "/v2/actors/alice/tokens",
        json={"label": "alice's laptop"},
    )
    assert r.status_code == 201
    issued = r.json()
    assert issued["actor_handle"] == "@alice"
    assert issued["label"] == "alice's laptop"
    assert len(issued["token"]) > 32  # plaintext present
    token = issued["token"]
    # listing exposes metadata but never the plaintext
    r2 = client.get("/v2/actors/alice/tokens")
    assert r2.status_code == 200
    items = r2.json()["tokens"]
    assert len(items) == 1
    assert items[0]["id"] == issued["id"]
    assert "token" not in items[0]
    assert items[0]["revoked_at"] is None


def test_token_binds_handle_for_speech_post(client):
    """A request that supplies X-Actor-Token + claims the bound
    handle is accepted. A request that supplies the same token but
    claims a *different* handle is rejected with 403."""
    # Issue tokens for alice and bob
    alice_token = client.post(
        "/v2/actors/alice/tokens", json={},
    ).json()["token"]
    client.post("/v2/actors/bob/tokens", json={})

    # Open op as alice (using alice's token) — accepted
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": alice_token},
        json={
            "space_id": "actor-token-thread",
            "kind": "inquiry",
            "title": "alice opens",
            "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 201, r.text
    op_id = r.json()["id"]

    # alice's token claims to be bob — rejected
    r = client.post(
        f"/v2/operations/{op_id}/events",
        headers={"X-Actor-Token": alice_token},
        json={
            "actor_handle": "@bob",
            "kind": "speech.claim",
            "payload": {"text": "impersonating bob"},
        },
    )
    assert r.status_code == 403, r.text
    assert "bound to" in r.text.lower() or "cannot speak as" in r.text.lower()


def test_revoked_token_is_rejected(client):
    """After revoke, the token no longer authenticates the handle."""
    issued = client.post("/v2/actors/alice/tokens", json={}).json()
    token = issued["token"]
    # Token works first
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": token},
        json={
            "space_id": "actor-token-thread",
            "kind": "inquiry",
            "title": "before revoke",
            "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 201
    # Revoke
    r = client.post(f"/v2/actors/alice/tokens/{issued['id']}/revoke")
    assert r.status_code == 200
    # Same token now fails
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": token},
        json={
            "space_id": "actor-token-thread",
            "kind": "inquiry",
            "title": "after revoke",
            "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 401, r.text
    assert "invalid or revoked" in r.text.lower()


def test_legacy_mode_without_token_still_works_when_not_required(client):
    """Without X-Actor-Token AND without BRIDGE_REQUIRE_ACTOR_TOKEN,
    the legacy shared-bearer + asserted-handle flow remains valid.
    Backward compat — no migration cliff."""
    r = client.post(
        "/v2/operations",
        json={
            "space_id": "actor-token-thread",
            "kind": "inquiry",
            "title": "legacy mode",
            "opener_actor_handle": "@nobody",
        },
    )
    assert r.status_code == 201, r.text


def test_strict_mode_demands_actor_token(client, monkeypatch):
    """When BRIDGE_REQUIRE_ACTOR_TOKEN=1, every actor-claiming request
    MUST carry X-Actor-Token. Legacy mode falls off."""
    monkeypatch.setenv("BRIDGE_REQUIRE_ACTOR_TOKEN", "1")
    r = client.post(
        "/v2/operations",
        json={
            "space_id": "actor-token-thread",
            "kind": "inquiry",
            "title": "strict mode",
            "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 401, r.text
    assert "x-actor-token" in r.text.lower()
