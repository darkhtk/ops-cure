"""Conformance: W3C traceparent. Spec §5."""
import re

import pytest

_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@pytest.mark.conformance_required
def test_response_carries_traceparent_when_request_has_none(client):
    r = client.get("/v3/schema/types")
    tp = r.headers.get("traceparent")
    assert tp is not None
    assert _PATTERN.match(tp), f"malformed traceparent: {tp!r}"


@pytest.mark.conformance_required
def test_inbound_traceparent_trace_id_preserved(client):
    inbound = "00-0123456789abcdef0123456789abcdef-fedcba9876543210-01"
    r = client.get(
        "/v3/schema/types",
        headers={"traceparent": inbound},
    )
    out = r.headers.get("traceparent")
    assert out is not None
    m = _PATTERN.match(out)
    assert m is not None
    assert m.group(1) == "0123456789abcdef0123456789abcdef"


@pytest.mark.conformance_required
def test_each_request_gets_distinct_span_id(client):
    same_trace = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1111111111111111-01"
    r1 = client.get("/v3/schema/types", headers={"traceparent": same_trace})
    r2 = client.get("/v3/schema/types", headers={"traceparent": same_trace})
    m1 = _PATTERN.match(r1.headers["traceparent"])
    m2 = _PATTERN.match(r2.headers["traceparent"])
    assert m1.group(1) == m2.group(1)
    assert m1.group(2) != m2.group(2)
