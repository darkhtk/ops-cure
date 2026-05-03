"""Conformance: schema discovery.

Spec §2 + §6.5 + §13. The bridge MUST publish the canonical type
schemas + an OpenAPI-public doc filtered to the v3 protocol surface.
"""
import pytest


@pytest.mark.conformance_required
def test_schema_types_lists_all_canonical_types(client):
    r = client.get("/v3/schema/types")
    assert r.status_code == 200
    body = r.json()
    schemas = body["schemas"]
    for name in ("OperationPolicy", "ExpectedResponse", "SpeechKinds", "PolicyErrorCodes"):
        assert name in schemas, f"missing schema: {name}"


@pytest.mark.conformance_required
def test_schema_types_lists_required_speech_kinds(client):
    """v3.1 minimum vocabulary."""
    r = client.get("/v3/schema/types")
    enum = set(r.json()["schemas"]["SpeechKinds"]["enum"])
    required = {
        "claim", "question", "answer", "propose", "agree", "object",
        "evidence", "block", "defer", "summarize", "react",
        "move_close", "ratify", "invite", "join",
    }
    missing = required - enum
    assert not missing, f"impl missing required speech kinds: {sorted(missing)}"


@pytest.mark.conformance_required
def test_schema_types_lists_required_close_policies(client):
    r = client.get("/v3/schema/types")
    enum = set(
        r.json()["schemas"]["OperationPolicy"]["properties"]["close_policy"]["enum"]
    )
    required = {
        "opener_unilateral", "any_participant", "operator_ratifies", "quorum",
    }
    missing = required - enum
    assert not missing, f"impl missing required close policies: {sorted(missing)}"


@pytest.mark.conformance_required
def test_schema_types_lists_required_error_codes(client):
    r = client.get("/v3/schema/types")
    enum = set(r.json()["schemas"]["PolicyErrorCodes"]["enum"])
    required = {
        "policy.max_rounds_exhausted",
        "policy.reply_kind_rejected",
        "policy.close_needs_operator_ratify",
        "policy.close_needs_quorum",
        "policy.close_needs_participant",
        "policy.join_invite_only",
        "policy.invite_needs_participant",
    }
    missing = required - enum
    assert not missing, f"impl missing required error codes: {sorted(missing)}"


@pytest.mark.conformance_required
def test_openapi_public_excludes_internal_endpoints(client):
    """Spec §15: internal endpoints MUST NOT appear in the public
    OpenAPI doc."""
    r = client.get("/v3/schema/openapi-public")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for p in paths:
        assert not p.startswith("/api/remote-claude/"), (
            f"internal endpoint leaked into public OpenAPI: {p}"
        )
        assert not p.startswith("/api/chat/"), (
            f"v1 chat endpoint leaked into public OpenAPI: {p}"
        )
