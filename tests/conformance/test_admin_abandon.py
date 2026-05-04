"""Phase 14: POST /v2/operations/{id}/_admin/abandon — admin-only force-close."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.conformance_required


def _open_op(client, space_id, *, policy: dict, kind: str = "task", title: str = "t"):
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id,
            "kind": kind,
            "title": title,
            "opener_actor_handle": "@alice",
            "objective": "x",
            "policy": policy,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_admin_abandon_force_closes_operator_ratifies_op(client, space_id):
    """An op whose close_policy=operator_ratifies cannot normally be
    closed without an operator-role ratify. The admin endpoint
    bypasses all of that and closes as abandoned."""
    op_id = _open_op(
        client, space_id,
        policy={"close_policy": "operator_ratifies"},
    )

    # Sanity: normal close path is blocked.
    blocked = client.post(
        f"/v2/operations/{op_id}/close",
        json={"actor_handle": "@alice", "resolution": "abandoned",
              "summary": "no-op"},
    )
    assert blocked.status_code == 400, blocked.text
    assert "policy" in blocked.json().get("detail", "")

    # Admin abandon succeeds.
    r = client.post(
        f"/v2/operations/{op_id}/_admin/abandon",
        json={"summary": "test cleanup"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "closed"
    assert body["resolution"] == "abandoned"


def test_admin_abandon_force_closes_quorum_op(client, space_id):
    op_id = _open_op(
        client, space_id,
        policy={"close_policy": "quorum", "min_ratifiers": 3},
    )
    r = client.post(
        f"/v2/operations/{op_id}/_admin/abandon",
        json={"summary": "orphaned quorum op"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "closed"
    assert r.json()["resolution"] == "abandoned"


def test_admin_abandon_unknown_op_returns_404(client, space_id):
    r = client.post(
        "/v2/operations/00000000-0000-0000-0000-000000000000/_admin/abandon",
        json={},
    )
    assert r.status_code == 404


def test_admin_abandon_idempotent_on_already_closed(client, space_id):
    """Closing an already-closed op surfaces 400, not 500 or silent
    success. The operator can tell the op was already settled."""
    op_id = _open_op(
        client, space_id,
        policy={"close_policy": "opener_unilateral"},
    )
    # Normal close first.
    r1 = client.post(
        f"/v2/operations/{op_id}/close",
        json={"actor_handle": "@alice", "resolution": "abandoned"},
    )
    assert r1.status_code == 200, r1.text
    # Second call via admin endpoint should fail with a state error.
    r2 = client.post(
        f"/v2/operations/{op_id}/_admin/abandon",
        json={},
    )
    assert r2.status_code == 400, r2.text
