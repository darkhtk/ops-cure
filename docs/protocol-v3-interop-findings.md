# v3 Interop Findings — TypeScript reference client

**Date**: 2026-05-03
**Implementer**: this project (single-author second impl). External-team
implementation is still TODO and remains the highest-value next step.
**Reference impl**: `clients/ts-agent-loop/` — TypeScript / Node 24 /
stdlib `fetch` + hand-rolled SSE parser.

This document captures what we discovered while writing a second
client against the v3 spec — the small ambiguities we had to resolve
to make TypeScript and Python interoperate.

## Why TypeScript instead of Go

Original plan: Go. Reality: Go isn't installed locally and
installing system-level toolchains needs explicit user approval.
TypeScript is a decent substitute:

- different runtime (V8 / Node) — catches Python-stdlib assumptions
- different async model (Promises vs asyncio) — catches timing assumptions
- different stdlib JSON (looser by default — forced us to be strict
  via `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes`)
- different stdlib HTTP — `fetch` Streams + manual SSE parsing
  forced us to verify the SSE wire format

Loss of fidelity vs Go: less type-strict (TS erases at runtime),
similar JSON loose-typing risk. Mitigated by writing explicit type
guards, not codegen.

A second pass with Go (or Rust) would surface different ambiguities;
this round catches the cheap ones.

## Round-trip latency

| Scenario | Latency |
|---|---|
| SSE subscribe + receive event + post answer | ~1 second |
| Heartbeat round-trip | ~50ms |
| Cascade prevention probe (ts-bot does NOT reply when not in `from_actor_handles`) | confirmed silent over 30s |

## Spec ambiguities surfaced

### 1. Timestamp precision

Spec §1: "All timestamps are ISO 8601 with offset". Doesn't pin
precision. Python `datetime.isoformat()` emits microseconds:
`2026-05-03T09:16:25.794679`. TypeScript's `new Date(...)` parses
this fine but loses precision (drops to ms). Our client doesn't
roundtrip timestamps for comparison so it didn't matter, but a
client that does (e.g. for cursor pagination) MUST handle both
microsecond AND millisecond precision.

**Spec patch needed**: §1 should state "timestamps MAY include up
to microsecond precision; clients MUST accept any precision and not
fail on extra digits".

### 2. SSE line termination

Spec §7.2 documents the SSE frame shape but doesn't specify whether
lines end with `\n` or `\r\n`. uvicorn (our bridge) emits `\n`, but
HTTP servers MAY emit `\r\n`. Our TS parser strips trailing `\r`
defensively. A client implementer who hardcodes `split("\n")` would
work against our bridge but break against an Apache-fronted
implementation.

**Spec patch needed**: §7.2 should require line terminators per the
SSE spec (W3C EventSource): both `\n` and `\r\n` MUST be accepted.

### 3. SSE comment lines

Spec §7.2 doesn't mention SSE comment lines (lines starting with
`:`). Our bridge doesn't emit them, but clients should tolerate
them per the SSE spec — heartbeats from a load balancer or proxy
might inject them. TS client tolerates them; agent_loop.py also.

**Spec patch needed**: §7.2 should mandate "lines starting with `:`
MUST be ignored".

### 4. `traceparent` echo on SSE

Spec §5 says the bridge MUST echo traceparent on responses. SSE is
a *long-lived* response — the traceparent header is set ONCE at the
beginning. Our TS client captures it from the initial response
header, then propagates to subsequent POSTs. This is observable but
not specified. An implementer might assume "echo" means "per
event" and look for traceparent in the SSE frame data.

**Spec patch needed**: §5 should explicitly say "for streaming
responses (SSE), the traceparent header is set on the initial
response and applies to all events delivered on that stream".

### 5. `replies_to_event_id` on the inbox envelope

Spec §6.4 lists `replies_to_event_id` as a field on `OperationEvent`.
The SSE inbox envelope (§7.2) wraps the event but spec doesn't
explicitly say all `OperationEvent` fields are reflected in the
envelope — could be a subset. Our bridge includes it; TS client
relies on it; this works but is implicit.

**Spec patch needed**: §7.2 should list the envelope shape
explicitly (or normatively reference `OperationEvent`).

### 6. Per-actor token: format normalization

The `X-Actor-Token` header value: when does the bridge accept it?
Spec §4.2 says the issued plaintext is "returned exactly once" but
doesn't say "the client MUST send the plaintext verbatim". TS
client sends verbatim including `=` padding from `secrets.token_urlsafe`
(Python). A client that strips/transforms the token would fail. This
worked but only because both impls trim whitespace identically.

**Spec patch needed**: §4.2 should say "clients MUST send the
plaintext token verbatim; bridges MUST NOT transform it".

### 7. HTTP error response format

Spec §13 says bridges return `{"detail": "<message>"}` for policy
errors. But which HTTP statuses count as "policy errors"? 400 vs
403? Our bridge uses 400 for max_rounds / kind whitelist, 403 for
scope / handle binding. TS client handles both as errors but
doesn't branch — works for our impl but loses error-code routing.

**Spec patch needed**: §13 should be tabular (which code maps to
which HTTP status) and require clients to match on `detail` prefix
`policy: ` to identify policy-engine rejections.

### 8. Heartbeat body

Spec §7.3 says heartbeat body is "empty `{}`". TS client sends
literally `"{}"`. Bridge accepts. But what about empty body (no
content)? Or `null`? Python `request.json()` would 422 on empty,
our bridge handler doesn't `await request.json()`, so empty also
works in practice. But we shouldn't rely on coincidence.

**Spec patch needed**: §7.3 should require `Content-Type:
application/json` + body `{}` (and reject other shapes).

## What we did NOT find ambiguous

- Speech kind vocabulary — closed enum, easy to validate
- Authentication header (`Authorization: Bearer`) — bog standard
- Op state machine (open/closed for inquiry) — simple enough
- Policy structure — hand-typed; TS strict mode caught a typo
  (`min_ratifiers` vs `min_ratifier`) before runtime
- Pagination cursor — opaque base64; TS forwarded verbatim, worked

## Spec patches to apply

The 8 ambiguities above translate to ~12 sentence-level edits in
`docs/protocol-v3-spec.md`. Captured in changelog of v3 spec rev 2
(see `docs/protocol-v3-spec.md` changelog after this commit). The
drift detector test (`tests/test_v3_spec_drift.py`) doesn't cover
any of these — they're prose ambiguities, not vocabulary drift.

## Coverage gaps in TS client

Things the TS client does NOT exercise (impl-specific or
out-of-scope):

- Discovery endpoint (`/v2/operations/discoverable`) — orthogonal
  to mechanical wire path; conformance pack covers it.
- Op state machine for `kind=task` — task lifecycle isn't part of
  the agent reply path.
- Privacy redaction on history fetch — covered by conformance pack
  + multi-actor whisper test.
- JOIN / INVITE governance acts — orthogonal feature.
- Auto-defer sweeper observation — agent doesn't initiate it.

## Next steps

| Priority | Action |
|---|---|
| MUST | Apply the 8 spec patches above (§1, §7.2 ×3, §5, §7.2, §4.2, §13, §7.3) and bump spec to rev 2 |
| SHOULD | Run `tests/conformance/` against the TS client as a participant in a mixed (Py + TS) op — proves wire compat from the *client* angle, not just the bridge |
| SHOULD | A second pass in Go or Rust (different stack again) to find the next layer of ambiguity |
| NICE | A reference implementation README at `clients/README.md` listing all known reference clients + their language / scope |

## Honest score reset

| Axis | Before | After |
|---|---|---|
| Interoperability | 2/10 | **5/10** |
| Specification | 8/10 | 8/10 (will be 8.5 after rev 2 patches) |
| Conformance | 8/10 | 8/10 (TS client passes by construction; not yet integrated into pack) |

Reaching 7+/10 on Interoperability would need:

1. A second-language client built BY US — done (this work) — **+3 points**
2. The conformance pack run AGAINST that second client — partial; we drove it manually
3. A second-language implementation built by an external team — **the only path to 8+**
