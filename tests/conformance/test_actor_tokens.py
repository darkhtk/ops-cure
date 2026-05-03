"""Conformance: per-actor tokens + handle binding. Spec §4."""
import pytest


@pytest.mark.conformance_required
def test_issue_returns_plaintext_once(client):
    r = client.post("/v2/actors/conformance-A/tokens", json={})
    assert r.status_code == 201
    body = r.json()
    assert body["actor_handle"] == "@conformance-A"
    assert isinstance(body.get("token"), str) and len(body["token"]) > 32


@pytest.mark.conformance_required
def test_list_does_not_expose_plaintext(client):
    client.post("/v2/actors/conformance-B/tokens", json={})
    r = client.get("/v2/actors/conformance-B/tokens")
    assert r.status_code == 200
    for item in r.json()["tokens"]:
        assert "token" not in item


@pytest.mark.conformance_required
def test_token_handle_binding_blocks_impersonation(client, space_id):
    """Token bound to A claiming to be B → HTTP 403."""
    issued = client.post("/v2/actors/conformance-C/tokens", json={}).json()
    token = issued["token"]
    # First open an op as A so we have something to post events into
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": token},
        json={
            "space_id": space_id, "kind": "inquiry", "title": "binding test",
            "opener_actor_handle": "@conformance-C",
        },
    )
    assert r.status_code == 201, r.text
    op_id = r.json()["id"]
    # Now A's token tries to post as B
    r = client.post(
        f"/v2/operations/{op_id}/events",
        headers={"X-Actor-Token": token},
        json={
            "actor_handle": "@conformance-D",
            "kind": "speech.claim",
            "payload": {"text": "impersonating"},
        },
    )
    assert r.status_code == 403


@pytest.mark.conformance_required
def test_revoked_token_rejected(client, space_id):
    issued = client.post("/v2/actors/conformance-E/tokens", json={}).json()
    token = issued["token"]
    # Token works
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": token},
        json={
            "space_id": space_id, "kind": "inquiry", "title": "before revoke",
            "opener_actor_handle": "@conformance-E",
        },
    )
    assert r.status_code == 201
    # Revoke
    r = client.post(f"/v2/actors/conformance-E/tokens/{issued['id']}/revoke")
    assert r.status_code == 200
    # Same token now fails 401
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": token},
        json={
            "space_id": space_id, "kind": "inquiry", "title": "after revoke",
            "opener_actor_handle": "@conformance-E",
        },
    )
    assert r.status_code == 401


@pytest.mark.conformance_required
def test_token_scope_read_only_blocks_mutations(client, space_id):
    issued = client.post(
        "/v2/actors/conformance-F/tokens",
        json={"scope": "read-only"},
    ).json()
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": issued["token"]},
        json={
            "space_id": space_id, "kind": "inquiry",
            "title": "ro mutation attempt",
            "opener_actor_handle": "@conformance-F",
        },
    )
    assert r.status_code == 403


@pytest.mark.conformance_required
def test_token_scope_speak_allows_mutations(client, space_id):
    issued = client.post(
        "/v2/actors/conformance-G/tokens",
        json={"scope": "speak"},
    ).json()
    r = client.post(
        "/v2/operations",
        headers={"X-Actor-Token": issued["token"]},
        json={
            "space_id": space_id, "kind": "inquiry", "title": "speak ok",
            "opener_actor_handle": "@conformance-G",
        },
    )
    assert r.status_code == 201
