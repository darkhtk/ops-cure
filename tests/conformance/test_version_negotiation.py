"""Conformance: protocol version negotiation. Spec §3."""
import pytest


@pytest.mark.conformance_required
def test_response_advertises_supported_versions(client):
    r = client.get("/v3/schema/types")
    supported = r.headers.get("X-Protocol-Version-Supported")
    assert supported is not None
    versions = {v.strip() for v in supported.split(",")}
    # 3.1 is the floor — every conformant impl supports at least it
    assert "3.1" in versions


@pytest.mark.conformance_required
def test_unknown_version_rejected_with_negotiation_payload(client):
    r = client.get(
        "/v3/schema/types",
        headers={"X-Protocol-Version": "99.0"},
    )
    assert r.status_code == 400
    body = r.json()
    assert "supported" in body
    assert "current" in body
    assert "3.1" in body["supported"]


@pytest.mark.conformance_required
def test_known_version_accepted(client):
    r = client.get(
        "/v3/schema/types",
        headers={"X-Protocol-Version": "3.1"},
    )
    assert r.status_code == 200
