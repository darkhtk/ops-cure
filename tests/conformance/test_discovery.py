"""Conformance: discovery + heartbeat. Spec §7.1.discoverable, §7.3."""
import pytest


def _open_op(client, *, space_id, title, policy=None, opener="@alice"):
    body = {
        "space_id": space_id, "kind": "inquiry",
        "title": title, "opener_actor_handle": opener,
    }
    if policy is not None:
        body["policy"] = policy
    r = client.post("/v2/operations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.conformance_required
def test_discoverable_returns_open_self_or_invite_ops(client, space_id):
    op_id = _open_op(client, space_id=space_id, title="discoverable default")
    r = client.get("/v2/operations/discoverable", params={"for": "@bob"})
    assert r.status_code == 200
    visible = {item["id"] for item in r.json()["items"]}
    assert op_id in visible


@pytest.mark.conformance_required
def test_discoverable_excludes_invite_only_for_outsiders(client, space_id):
    op_id = _open_op(
        client, space_id=space_id, title="invite-only",
        policy={"join_policy": "invite_only"},
    )
    r = client.get("/v2/operations/discoverable", params={"for": "@stranger"})
    visible = {item["id"] for item in r.json()["items"]}
    assert op_id not in visible


@pytest.mark.conformance_required
def test_discoverable_excludes_ops_already_in(client, space_id):
    op_id = _open_op(client, space_id=space_id, title="alice's own op", opener="@alice")
    r = client.get("/v2/operations/discoverable", params={"for": "@alice"})
    visible = {item["id"] for item in r.json()["items"]}
    assert op_id not in visible


@pytest.mark.conformance_required
def test_discoverable_pagination_cursor_returns_next_page(client, space_id):
    op_ids = [
        _open_op(client, space_id=space_id, title=f"op {i}")
        for i in range(4)
    ]
    r = client.get(
        "/v2/operations/discoverable",
        params={"for": "@bob", "limit": 2},
    )
    body = r.json()
    page1 = {it["id"] for it in body["items"]}
    cursor = body["next_cursor"]
    assert cursor is not None
    seen = set(page1)
    while cursor:
        r = client.get(
            "/v2/operations/discoverable",
            params={"for": "@bob", "limit": 2, "cursor": cursor},
        )
        body = r.json()
        for it in body["items"]:
            seen.add(it["id"])
        cursor = body["next_cursor"]
    for op_id in op_ids:
        assert op_id in seen


@pytest.mark.conformance_required
def test_heartbeat_creates_actor_and_updates_last_seen(client):
    r = client.post("/v2/actors/conformance-heart/heartbeat")
    assert r.status_code == 200
    body = r.json()
    assert body["actor_handle"] == "@conformance-heart"
    assert body["last_seen_at"]
