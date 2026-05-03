"""v3 phase 2.5 — /v2/operations/discoverable endpoint.

Closes the discovery gap from the mid-collab join review:
'agents can JOIN ops, but how do they find ops to join?'

The endpoint returns ops the asker is **not yet a participant of**
but **could legitimately join** under that op's join_policy.
"""
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
    # Provision a fixed thread for tests
    from app.behaviors.chat.models import ChatThreadModel
    with db.session_scope() as s:
        t = ChatThreadModel(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="discoverable-thread", title="t", created_by="alice",
        )
        s.add(t)
    with TestClient(fastapi_app) as c:
        c.headers.update({"Authorization": "Bearer t"})
        yield c


def _open_op(client, *, title, policy=None, opener="@alice"):
    body = {
        "space_id": "discoverable-thread",
        "kind": "inquiry",
        "title": title,
        "opener_actor_handle": opener,
    }
    if policy is not None:
        body["policy"] = policy
    r = client.post("/v2/operations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_discoverable_lists_open_ops_actor_can_join(client):
    """An actor with no prior participation sees self_or_invite +
    open ops, but not invite_only ops."""
    op_self = _open_op(client, title="self_or_invite default")
    op_open = _open_op(
        client, title="open policy",
        policy={"join_policy": "open"},
    )
    op_invite_only = _open_op(
        client, title="invite_only",
        policy={"join_policy": "invite_only"},
    )

    r = client.get("/v2/operations/discoverable", params={"for": "@bob"})
    assert r.status_code == 200, r.text
    data = r.json()
    visible_ids = {item["id"] for item in data["items"]}
    assert op_self in visible_ids
    assert op_open in visible_ids
    assert op_invite_only not in visible_ids
    assert data["actor_handle"] == "@bob"


def test_discoverable_excludes_ops_actor_already_in(client):
    """alice opens an op (becomes opener participant). The endpoint
    does NOT surface that op to alice — she's already in it."""
    op_id = _open_op(client, title="alice's own op", opener="@alice")
    r = client.get("/v2/operations/discoverable", params={"for": "@alice"})
    assert r.status_code == 200
    visible_ids = {item["id"] for item in r.json()["items"]}
    assert op_id not in visible_ids


def test_discoverable_excludes_closed_ops(client):
    op_id = _open_op(client, title="will close")
    # close it
    r = client.post(
        f"/v2/operations/{op_id}/close",
        json={"actor_handle": "@alice", "resolution": "answered"},
    )
    assert r.status_code == 200
    # bob (uninvolved) should not see it as discoverable
    r = client.get("/v2/operations/discoverable", params={"for": "@bob"})
    visible_ids = {item["id"] for item in r.json()["items"]}
    assert op_id not in visible_ids


def test_discoverable_filters_by_space_id(client):
    """When space_id is given, only ops in that space are returned.

    Note: op.space_id is the kernel-internal `chat:<thread_uuid>`
    form, not the discord_thread_id the open endpoint accepts. The
    test reads the canonical space_id from the op response directly.
    """
    op_id = _open_op(client, title="our space")
    op = client.get(f"/v2/operations/{op_id}").json()
    canonical_space = op["space_id"]
    r = client.get(
        "/v2/operations/discoverable",
        params={"for": "@bob", "space_id": canonical_space},
    )
    assert r.status_code == 200
    visible_ids = {item["id"] for item in r.json()["items"]}
    assert op_id in visible_ids
    # Wrong space → empty
    r = client.get(
        "/v2/operations/discoverable",
        params={"for": "@bob", "space_id": "nonexistent-space"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_discoverable_unknown_actor_still_returns_self_join_ops(client):
    """An actor with no row yet still gets self_or_invite + open ops
    (they could create the actor by subscribing to inbox SSE later)."""
    op_id = _open_op(client, title="self join allowed")
    r = client.get("/v2/operations/discoverable", params={"for": "@brand-new-actor"})
    assert r.status_code == 200
    visible_ids = {item["id"] for item in r.json()["items"]}
    assert op_id in visible_ids
