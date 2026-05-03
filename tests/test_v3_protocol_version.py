"""v3 phase 4 — protocol version negotiation."""
from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setenv("BRIDGE_POLICY_SWEEPER_SECONDS", "0")
    monkeypatch.delenv("BRIDGE_REQUIRE_PROTOCOL_VERSION", raising=False)
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
    from app.protocol_version import _reset_for_tests
    _reset_for_tests()
    with TestClient(fastapi_app) as c:
        c.headers.update({"Authorization": "Bearer t"})
        yield c


def test_response_advertises_supported_versions(client):
    r = client.get("/v3/schema/types")
    assert r.headers.get("X-Protocol-Version-Supported") == "3.0, 3.1"
    assert r.headers.get("X-Protocol-Version-Current") == "3.1"


def test_unknown_version_is_rejected_with_negotiation_payload(client):
    r = client.get(
        "/v3/schema/types",
        headers={"X-Protocol-Version": "99.0"},
    )
    assert r.status_code == 400
    body = r.json()
    assert "supported" in body
    assert "3.1" in body["supported"]
    assert body["current"] == "3.1"
    # Negotiation headers are still on the rejection
    assert r.headers.get("X-Protocol-Version-Supported")


def test_known_version_is_accepted(client):
    r = client.get(
        "/v3/schema/types",
        headers={"X-Protocol-Version": "3.0"},
    )
    assert r.status_code == 200


def test_strict_mode_rejects_missing_header(client, monkeypatch):
    monkeypatch.setenv("BRIDGE_REQUIRE_PROTOCOL_VERSION", "1")
    r = client.get("/v3/schema/types")
    assert r.status_code == 400
    assert "header is required" in r.json()["detail"].lower()


def test_strict_mode_does_not_break_non_protocol_paths(client, monkeypatch):
    monkeypatch.setenv("BRIDGE_REQUIRE_PROTOCOL_VERSION", "1")
    # health is not under /v2/ or /v3/ → not gated
    r = client.get("/healthz")
    assert r.status_code in (200, 404)  # whichever shape exists
    # but /v3 paths ARE gated
    r2 = client.get("/v3/schema/types")
    assert r2.status_code == 400


def test_usage_counter_tracks_per_version(client):
    """Visibility into who's on what version. Used to decide when to
    retire an older minor."""
    from app.protocol_version import usage_counts, _reset_for_tests
    _reset_for_tests()
    client.get("/v3/schema/types", headers={"X-Protocol-Version": "3.0"})
    client.get("/v3/schema/types", headers={"X-Protocol-Version": "3.0"})
    client.get("/v3/schema/types", headers={"X-Protocol-Version": "3.1"})
    client.get("/v3/schema/types")  # default = current
    counts = usage_counts()
    assert counts.get("3.0") == 2
    assert counts.get("3.1") == 2  # one explicit, one default
