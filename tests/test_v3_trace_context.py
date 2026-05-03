"""v3 phase 4 — W3C traceparent propagation."""
from __future__ import annotations

import re
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


_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def test_response_carries_traceparent_when_request_has_none(client):
    """Bridge generates a traceparent if the caller didn't send one."""
    r = client.get("/v3/schema/types")
    tp = r.headers.get("traceparent")
    assert tp is not None
    assert _PATTERN.match(tp)


def test_inbound_traceparent_trace_id_is_preserved(client):
    """When caller supplies a traceparent, the bridge keeps the
    trace_id (span_id may change — bridge generates its own span)."""
    inbound = "00-0123456789abcdef0123456789abcdef-fedcba9876543210-01"
    r = client.get(
        "/v3/schema/types",
        headers={"traceparent": inbound},
    )
    out = r.headers.get("traceparent")
    assert out is not None
    m = _PATTERN.match(out)
    assert m
    assert m.group(1) == "0123456789abcdef0123456789abcdef"


def test_malformed_traceparent_falls_back_to_generated(client):
    r = client.get(
        "/v3/schema/types",
        headers={"traceparent": "garbage"},
    )
    out = r.headers.get("traceparent")
    assert _PATTERN.match(out)


def test_each_request_gets_distinct_span_id(client):
    """Even within the same trace, span_ids are per-request fresh."""
    r1 = client.get(
        "/v3/schema/types",
        headers={"traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1111111111111111-01"},
    )
    r2 = client.get(
        "/v3/schema/types",
        headers={"traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-2222222222222222-01"},
    )
    m1 = _PATTERN.match(r1.headers["traceparent"])
    m2 = _PATTERN.match(r2.headers["traceparent"])
    assert m1.group(1) == m2.group(1)  # same trace
    assert m1.group(2) != m2.group(2)  # different spans


def test_log_filter_attaches_trace_context(client, caplog):
    """The TraceContextLogFilter copies trace_id/span_id onto every
    log record emitted during a request handler, so downstream log
    consumers can correlate without joining HTTP access logs."""
    import logging
    inbound = "00-cafebabecafebabecafebabecafebabe-1234567890abcdef-01"
    with caplog.at_level(logging.DEBUG):
        client.get(
            "/v3/schema/types",
            headers={"traceparent": inbound},
        )
    # At least one record should carry the inbound trace_id
    found = any(
        getattr(r, "trace_id", None) == "cafebabecafebabecafebabecafebabe"
        for r in caplog.records
    )
    assert found, (
        "expected at least one log record to carry the inbound trace_id; "
        f"records were: {[(r.name, getattr(r, 'trace_id', None)) for r in caplog.records]}"
    )
