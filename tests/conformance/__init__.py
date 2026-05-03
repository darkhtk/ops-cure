"""v3 conformance test pack.

Any conformant implementation of the Opscure Bridge v3 protocol
(see ``docs/protocol-v3-spec.md``) must pass the tests under this
package. The pack is intentionally HTTP-only — it does NOT import
any bridge implementation modules — so it can validate alternative
servers / clients written in other languages or stacks.

Two run modes:

  - **In-process** (default): a TestClient is constructed from the
    Opscure FastAPI app for fast CI. Used to prove that *our* impl
    passes its own conformance pack.

  - **Live**: set ``BRIDGE_CONFORMANCE_BASE_URL=http://host:port``
    and the suite hits that endpoint via httpx. Used to validate a
    foreign implementation. The bridge under test must run with
    ``BRIDGE_TEST_MODE=1`` so the fixture endpoints
    (``/v2/_test/...``) are reachable.

Auth: the suite reads ``BRIDGE_SHARED_AUTH_TOKEN`` from the
environment (default ``conformance-shared``) and uses it as the
``Authorization: Bearer`` header on every request.

Markers:

  - ``conformance_required``: a passing implementation MUST pass
    these. Failure = non-conformant.
  - ``conformance_optional``: behavior is permitted to vary by impl
    (e.g. sweeper cadence, SSE heartbeat interval). A skipping impl
    MUST document why.
"""
