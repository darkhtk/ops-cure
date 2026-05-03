"""Conformance: §17 Bounds & quotas.

These tests pin the perimeter contract: an over-cap body, an
over-cap nesting, and an over-cap handler runtime each surface
through HTTP with a documented error code. ``BRIDGE_BOUNDS_LOG_ONLY=1``
inverts enforcement to observation only.
"""
from __future__ import annotations

import json
import os

import pytest


pytestmark = pytest.mark.conformance_required


# ---------------------------------------------------------------------------
# Body size — 413 / body.too_large
# ---------------------------------------------------------------------------


def test_body_size_over_cap_returns_413(client, space_id):
    """Send a JSON body larger than the default 1 MiB cap."""
    # Build a payload that's clearly > 1 MiB after JSON-encoding.
    big_blob = "x" * (1_100_000)
    payload = {
        "space_id": space_id,
        "kind": "inquiry",
        "title": "big",
        "opener_actor_handle": "@stress",
        "objective": big_blob,
    }
    body = json.dumps(payload).encode("utf-8")
    r = client.post(
        "/v2/operations",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413, r.text
    body_json = r.json()
    # Surface the stable code so clients can branch.
    assert body_json.get("code") == "body.too_large" or "body.too_large" in r.text


def test_body_size_under_cap_passes(client, space_id):
    """Sanity: a small open request still works."""
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id,
            "kind": "inquiry",
            "title": "small",
            "opener_actor_handle": "@alice",
            "objective": "tiny",
        },
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# JSON depth — 400 / payload.depth_exceeded
# ---------------------------------------------------------------------------


def _nested_dict(depth: int) -> dict:
    """Build a dict nested ``depth`` levels deep."""
    out: dict = {"x": 1}
    for _ in range(depth):
        out = {"n": out}
    return out


def test_payload_depth_over_cap_rejected_400(client, space_id):
    """policy field is a free-form dict — gets walked at POST entry."""
    # Default cap is 32 levels; send 50.
    nested = _nested_dict(50)
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id,
            "kind": "inquiry",
            "title": "deep",
            "opener_actor_handle": "@alice",
            "objective": "x",
            "policy": nested,
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "payload.depth_exceeded"


def test_payload_depth_under_cap_passes(client, space_id):
    """A flat policy dict must still be accepted."""
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id,
            "kind": "inquiry",
            "title": "flat",
            "opener_actor_handle": "@alice",
            "objective": "x",
            "policy": {"close_policy": "opener_unilateral"},
        },
    )
    assert r.status_code == 201, r.text


def test_event_payload_depth_over_cap_rejected(client, space_id):
    """Same cap applies on the events endpoint's free-form payload."""
    # First open an op to post an event against.
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id,
            "kind": "task",
            "title": "for-events",
            "opener_actor_handle": "@alice",
            "objective": "do thing",
        },
    )
    assert r.status_code == 201, r.text
    op_id = r.json()["id"]

    nested = _nested_dict(60)
    r2 = client.post(
        f"/v2/operations/{op_id}/events",
        json={
            "actor_handle": "@alice",
            "kind": "speech.claim",
            "payload": {"text": "hi", "metadata": nested},
        },
    )
    assert r2.status_code == 400, r2.text


# ---------------------------------------------------------------------------
# Spec contract — error codes documented
# ---------------------------------------------------------------------------


def test_error_codes_documented_in_spec():
    """Spec §13 must list the three new perimeter codes."""
    spec_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "docs", "protocol-v3-spec.md",
    )
    spec_path = os.path.normpath(spec_path)
    with open(spec_path, "r", encoding="utf-8") as f:
        text = f.read()
    for code in ("body.too_large", "request.timeout", "payload.depth_exceeded"):
        assert code in text, f"spec missing code {code!r}"
    assert "## 17. Bounds & quotas" in text, "spec missing §17 section"


# ---------------------------------------------------------------------------
# Log-only mode — observe-only rollout switch
# ---------------------------------------------------------------------------
# Need a separate client that's launched with BRIDGE_BOUNDS_LOG_ONLY=1.
# We override the standard fixture for these tests.


@pytest.fixture
def log_only_client(tmp_path, monkeypatch, base_url):
    if base_url is not None:
        pytest.skip("log-only mode test requires in-process bridge")
    import sys

    token = os.environ.get("BRIDGE_SHARED_AUTH_TOKEN", "conformance-shared")
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", token)
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv(
        "BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'lo.db').as_posix()}",
    )
    monkeypatch.setenv("BRIDGE_POLICY_SWEEPER_SECONDS", "0")
    monkeypatch.setenv("BRIDGE_TEST_MODE", "1")
    monkeypatch.setenv("BRIDGE_BOUNDS_LOG_ONLY", "1")
    NAS_BRIDGE_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "nas_bridge"),
    )
    if NAS_BRIDGE_ROOT not in sys.path:
        sys.path.insert(0, NAS_BRIDGE_ROOT)
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    import app.db as db
    db.init_db()
    with TestClient(fastapi_app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def test_log_only_mode_does_not_block_oversize_body(log_only_client):
    """With log-only, oversize bodies pass through (still parsed,
    may fail downstream for other reasons — but NOT 413)."""
    r = log_only_client.post("/v2/_test/provision-thread")
    if r.status_code == 404:
        pytest.skip("test-mode endpoint disabled")
    space_id = r.json()["space_id"]

    big = "x" * 1_100_000
    r2 = log_only_client.post(
        "/v2/operations",
        json={
            "space_id": space_id,
            "kind": "inquiry",
            "title": "log-only-big",
            "opener_actor_handle": "@alice",
            "objective": big,
        },
    )
    # MUST NOT be 413 — the cap is logged, not enforced.
    assert r2.status_code != 413, (
        f"log_only=1 should not return 413; got {r2.status_code}"
    )


def test_log_only_mode_does_not_block_deep_payload(log_only_client):
    r = log_only_client.post("/v2/_test/provision-thread")
    if r.status_code == 404:
        pytest.skip("test-mode endpoint disabled")
    space_id = r.json()["space_id"]

    nested = _nested_dict(60)
    r2 = log_only_client.post(
        "/v2/operations",
        json={
            "space_id": space_id,
            "kind": "inquiry",
            "title": "log-only-deep",
            "opener_actor_handle": "@alice",
            "objective": "x",
            "policy": nested,
        },
    )
    # log_only: must not surface payload.depth_exceeded.
    if r2.status_code == 400:
        body = r2.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("code") != "payload.depth_exceeded"
