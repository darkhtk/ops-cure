"""v3 phase 4 — actor heartbeat + presence diagnostics."""
from __future__ import annotations

import sys
import time
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
    with TestClient(fastapi_app) as c:
        c.headers.update({"Authorization": "Bearer t"})
        yield c


def test_heartbeat_creates_actor_and_updates_last_seen(client):
    r = client.post("/v2/actors/probe-A/heartbeat")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["actor_handle"] == "@probe-A"
    assert body["last_seen_at"]
    # The presence summary surfaces this actor with a recent timestamp.
    diag = client.get("/v2/diagnostics").json()
    handles = {p["handle"] for p in diag["actors"]["presence"]}
    assert "@probe-A" in handles


def test_heartbeat_with_bound_token_succeeds(client):
    """Token-bound heartbeat is the production path — agents have a
    token and ping with it. Verify the verify_actor_handle_claim
    gate accepts the matching handle."""
    issued = client.post("/v2/actors/probe-B/tokens", json={}).json()
    token = issued["token"]
    r = client.post(
        "/v2/actors/probe-B/heartbeat",
        headers={"X-Actor-Token": token},
    )
    assert r.status_code == 200


def test_heartbeat_with_wrong_handle_rejected(client):
    """A token bound to actor X cannot heartbeat for actor Y."""
    issued = client.post("/v2/actors/probe-C/tokens", json={}).json()
    token = issued["token"]
    r = client.post(
        "/v2/actors/probe-D/heartbeat",
        headers={"X-Actor-Token": token},
    )
    assert r.status_code == 403


def test_diagnostics_protocol_version_counter_present(client):
    """The diagnostics endpoint exposes the protocol-version usage
    counter (driven by the middleware). Used to decide when to
    retire a minor version."""
    client.get("/v3/schema/types", headers={"X-Protocol-Version": "3.0"})
    client.get("/v3/schema/types", headers={"X-Protocol-Version": "3.1"})
    diag = client.get("/v2/diagnostics").json()
    assert "protocol_versions" in diag
    # at minimum the entries we just exercised should be visible
    counts = diag["protocol_versions"]
    assert counts.get("3.0", 0) >= 1
    assert counts.get("3.1", 0) >= 1
