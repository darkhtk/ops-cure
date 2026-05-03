# Opscure Bridge v3 Conformance Pack

A pure-HTTP test suite that any v3 implementation
([protocol-v3-spec.md](../../docs/protocol-v3-spec.md)) MUST pass to
claim conformance.

## What this is

- **HTTP-only.** No imports from `app.*`. Tests use `httpx` (live)
  or `fastapi.testclient.TestClient` (in-process) — both surface the
  same minimum interface, so tests don't care which mode is active.
- **Implementation-agnostic.** A foreign server implementing the
  same paths + payload shapes can run this pack against itself.
- **Markers**: every test is `@pytest.mark.conformance_required`
  (fail = non-conformant). Optional / impl-specific behavior is
  marked separately.

## Running against our impl (in-process, fast — CI default)

```
python -m pytest tests/conformance/ -q
```

The conftest spins up a fresh in-tmpdir bridge per test. ~20s for
the full pack.

## Running against a live bridge

The bridge under test must be started with:

```
BRIDGE_SHARED_AUTH_TOKEN=<some-token>
BRIDGE_TEST_MODE=1                           # enables /v2/_test/*
BRIDGE_POLICY_SWEEPER_SECONDS=0              # optional, for deterministic timing
```

Then run the pack:

```
BRIDGE_CONFORMANCE_BASE_URL=http://192.168.0.10:18080 \
BRIDGE_SHARED_AUTH_TOKEN=<some-token> \
python -m pytest tests/conformance/ -q
```

**Do NOT run a production bridge with `BRIDGE_TEST_MODE=1`** — the
test fixture endpoint provisions threads under the
`conformance` guild prefix and is intentionally not gated by
auth-of-origin. Use a staging instance.

## Coverage at v3.1

| Area | Tests | Spec § |
|---|---|---|
| Schema discovery (types + filtered OpenAPI) | 5 | §2, §6.5, §13 |
| Version negotiation | 3 | §3 |
| Traceparent propagation | 3 | §5 |
| Per-actor token issue/list/revoke + binding | 6 | §4 |
| Token scope (read-only / speak / admin) | 2 (in actor_tokens) | §4.5 |
| Policy enforcement (max_rounds, kind whitelist, close, join) | 6 | §12 |
| Discovery + heartbeat | 5 | §7.1, §7.3 |
| Lifecycle (event log, reply chain, privacy) | 4 | §6.4, §10 |
| **Total required** | **32** | |

## What's NOT covered (intentionally)

- **Sweeper cadence.** `policy.by_round_seq` auto-DEFER timing
  varies per impl. Conformant impls MUST emit the defer event
  eventually, but exact latency is not pinned. Conformance pack
  asserts on the eventual state, not the wall-clock interval.
- **SSE heartbeat interval.** Bridge MAY pick any cadence in
  [1, 120]s as long as heartbeats fire. We don't time them.
- **Storage backend.** SQLite vs Postgres vs in-memory: opaque.
- **Concurrency limits.** Each impl picks its connection pool size.

## Adding a conformance test

1. Identify the spec § the test pins down.
2. The test MUST use only paths in `/v3/schema/openapi-public`.
3. Use the `client` fixture (HTTP adapter) and the `space_id`
   fixture (per-test fresh thread via the test-mode endpoint).
4. Mark with `@pytest.mark.conformance_required` if the spec uses
   normative language (MUST), `@pytest.mark.conformance_optional`
   otherwise.
5. Update this README's coverage table.
6. Update `docs/protocol-v3-spec.md` if you discovered an
   ambiguity — the spec wins; code follows.
