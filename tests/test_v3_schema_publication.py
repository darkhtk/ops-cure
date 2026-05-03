"""v3 phase 4 — schema publication endpoints.

`/v3/schema/types` exposes hand-curated JSON Schemas for the four
canonical protocol types (OperationPolicy, ExpectedResponse,
SpeechKinds, PolicyErrorCodes). `/v3/schema/openapi-public` exposes
the OpenAPI doc filtered to ``protocol-v3-public`` tagged endpoints.

Tests pin down:
- the schemas list the right vocabulary (so contract drift breaks
  publication, not just internal code)
- internal endpoints are filtered out
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
    with TestClient(fastapi_app) as c:
        c.headers.update({"Authorization": "Bearer t"})
        yield c


def test_schema_types_lists_all_four_canonical_types(client):
    r = client.get("/v3/schema/types")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "3.x"
    schemas = body["schemas"]
    assert set(schemas) == {
        "OperationPolicy", "ExpectedResponse", "SpeechKinds", "PolicyErrorCodes",
    }


def test_operation_policy_schema_lists_contract_close_policies(client):
    """The schema's enum must match the contract's frozenset.
    Drift here means a new policy was added to the contract but not
    documented in the published schema."""
    from app.kernel.v2 import contract as v2_contract
    r = client.get("/v3/schema/types")
    schema = r.json()["schemas"]["OperationPolicy"]
    enum = set(schema["properties"]["close_policy"]["enum"])
    assert enum == set(v2_contract.ALL_CLOSE_POLICIES)


def test_speech_kinds_schema_matches_contract(client):
    from app.kernel.v2 import contract as v2_contract
    r = client.get("/v3/schema/types")
    schema = r.json()["schemas"]["SpeechKinds"]
    assert set(schema["enum"]) == set(v2_contract.SPEECH_KINDS)


def test_error_codes_schema_includes_all_known(client):
    """All policy_engine.CODE_* constants must surface in the schema —
    otherwise an external consumer can't map a 400 to a stable code."""
    from app.kernel.v2 import (
        CODE_MAX_ROUNDS_EXHAUSTED, CODE_REPLY_KIND_REJECTED,
        CODE_CLOSE_NEEDS_OPERATOR, CODE_CLOSE_NEEDS_QUORUM,
        CODE_CLOSE_NEEDS_PARTICIPANT, CODE_JOIN_INVITE_ONLY,
        CODE_INVITE_NEEDS_PARTICIPANT,
    )
    r = client.get("/v3/schema/types")
    enum = set(r.json()["schemas"]["PolicyErrorCodes"]["enum"])
    expected = {
        CODE_MAX_ROUNDS_EXHAUSTED, CODE_REPLY_KIND_REJECTED,
        CODE_CLOSE_NEEDS_OPERATOR, CODE_CLOSE_NEEDS_QUORUM,
        CODE_CLOSE_NEEDS_PARTICIPANT, CODE_JOIN_INVITE_ONLY,
        CODE_INVITE_NEEDS_PARTICIPANT,
    }
    assert enum == expected


def test_public_openapi_excludes_internal_endpoints(client):
    """Filter check: /api/remote-claude/* (internal) must NOT appear
    in the published doc; /v2/operations/{id}/events (public) must."""
    r = client.get("/v3/schema/openapi-public")
    assert r.status_code == 200
    paths = r.json()["paths"]
    # internal paths filtered out
    assert not any(p.startswith("/api/remote-claude/") for p in paths)
    # at least one v3-public endpoint kept
    assert any("/v2/operations" in p for p in paths)
    # actor token endpoints kept
    assert any("/v2/actors/" in p and "/tokens" in p for p in paths)


def test_public_openapi_each_kept_op_is_v3_public_tagged(client):
    """Every op surfaced in the filtered doc carries the v3-public
    tag. Sanity that the filter logic is correct."""
    r = client.get("/v3/schema/openapi-public")
    paths = r.json()["paths"]
    for _path, ops in paths.items():
        for _method, spec in ops.items():
            assert "protocol-v3-public" in spec.get("tags", []), (
                f"op missing v3-public tag: {_path} {_method}"
            )
