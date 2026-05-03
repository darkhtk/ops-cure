# Opscure Bridge Protocol v3 — Normative Specification

**Status**: Normative (rev 11, 2026-05-04). This document is the
authoritative description of Opscure Bridge protocol v3.x. Where it
disagrees with code, the spec is wrong and a clarifying patch is
welcome — but in the meantime, the **wire test fixtures**
(`tests/test_kernel_v3_*` and `tests/test_v3_*`) are the binding
behaviour.

**Audience**: implementers of v3 clients (agent runners, UI consumers,
alternative bridges). Reading order: §1–§4 (orientation), then jump
to §6 (types) and §7 (endpoints). §10–§12 (privacy, discovery,
policy) are essential before shipping a client; §13 (error codes) is
how a client interprets policy rejections.

**Out of scope**: the in-process kernel APIs (`V2Repository`,
`PolicyEngine`, etc.) — those are implementation detail. The
*protocol* is whatever a client sees over HTTP.

---

## 1. Conventions

- Keywords **MUST**, **SHOULD**, **MAY**, **MUST NOT**, **SHOULD NOT**
  follow [RFC 2119] semantics.
- All HTTP traffic is JSON over HTTP/1.1 except where noted (SSE
  uses `text/event-stream`).
- All UUIDs are lowercase RFC 4122 v4 strings.
- All timestamps are ISO 8601 with offset (`2026-05-03T01:00:00Z` or
  `2026-05-03T01:00:00+00:00`). Bridges **MUST** emit UTC; clients
  **MUST** accept any valid offset and normalize to UTC for storage.
  Timestamps **MAY** include up to microsecond precision (e.g.
  `2026-05-03T01:00:00.123456Z`); clients **MUST** accept any
  precision (or none) and **MUST NOT** fail on extra fractional
  digits beyond their internal granularity.
- All actor handles are strings prefixed with `@` (e.g. `@alice`).
  When a client supplies a handle without `@`, the bridge **MUST**
  treat it as if `@` were present. When a bridge emits a handle, it
  **MUST** include the `@` prefix.
- Field names use `snake_case`.
- A "speech kind" is the trailing token (e.g. `claim`); the wire
  representation in event payloads is the full `chat.speech.<kind>`
  string.

[RFC 2119]: https://www.rfc-editor.org/rfc/rfc2119

## 2. Protocol surface

The v3 protocol is the union of HTTP endpoints under prefixes `/v2/`
and `/v3/` that bear the OpenAPI tag `protocol-v3-public`. The
authoritative list is published at:

```
GET /v3/schema/openapi-public
```

Endpoints not bearing this tag are implementation-internal and
**MUST NOT** be relied on by v3 clients. Clients **SHOULD** dump the
above doc at startup and fail loudly if endpoints they depend on are
missing.

## 3. Versioning

### 3.1 Header negotiation

A client **MAY** declare its target version via the request header:

```
X-Protocol-Version: 3.1
```

Every response (including error responses) **MUST** carry:

```
X-Protocol-Version-Supported: 3.0, 3.1
X-Protocol-Version-Current: 3.1
```

### 3.2 Strict mode

When the bridge runs with `BRIDGE_REQUIRE_PROTOCOL_VERSION=1`, every
request to a `/v2/` or `/v3/` path **MUST** include
`X-Protocol-Version`. Requests without it are rejected with HTTP 400.

### 3.3 Bump rules

A change is **major** (v3 → v4) when it:

- removes an existing field
- adds a *required* field on an existing endpoint
- changes the semantics of an existing field
- removes a value from an existing closed enum

A change is **minor** (3.0 → 3.1) when it:

- adds a new optional field
- adds a new value to an existing closed enum (clients MUST tolerate)
- adds a new endpoint

A bridge **MUST NOT** retire a major version while clients are still
using it. The protocol-version usage counter exposed at
`/v2/diagnostics` is the authoritative signal of whether retirement
is safe.

### 3.4 Token format

Per-actor token plaintext (returned from `POST /v2/actors/{handle}/tokens`)
is an opaque string. Clients **MUST** send it verbatim in
`X-Actor-Token`; bridges **MUST NOT** transform / trim / normalize the
value before hashing.

### 3.5 Tolerating new minors

Clients **MUST** ignore unknown fields on responses (forward compat).
Clients **SHOULD** treat unknown speech kinds (§8) as opaque
chat.speech.* events and not throw — the kinds list is closed within
a major version but will grow within minors.

## 4. Authentication

### 4.1 Shared bearer

Every v3 request **MUST** carry:

```
Authorization: Bearer <BRIDGE_SHARED_AUTH_TOKEN>
```

This authenticates the *bridge caller* (operator-level access). It
does **not** authenticate the actor handle the caller asserts.

### 4.2 Per-actor tokens

A bridge **MAY** issue per-actor tokens that bind a token to a
specific actor handle. Issuance, listing, and revocation are at:

```
POST   /v2/actors/{handle}/tokens
GET    /v2/actors/{handle}/tokens
POST   /v2/actors/{handle}/tokens/{token_id}/revoke
```

All three require the shared bearer (a per-actor token cannot mint
or revoke its own).

The `POST` (issue) returns the plaintext token **exactly once**.
Bridges **MUST NOT** persist the plaintext; they store
`sha256(token)`.

The plaintext is supplied by clients on subsequent requests via:

```
X-Actor-Token: <plaintext>
```

### 4.3 Token-handle binding

When a request carries `X-Actor-Token` AND claims an actor handle
(in body field or `?actor_handle=` query):

- The bridge **MUST** look up the token's hash in `actor_tokens_v2`.
- If absent or revoked: HTTP 401 with `detail: "X-Actor-Token is
  invalid or revoked"`.
- If present but the bound handle ≠ claimed handle: HTTP 403 with
  `detail: "X-Actor-Token is bound to '<bound>', cannot speak as
  '<claimed>'"`.

### 4.4 Strict identity mode

When `BRIDGE_REQUIRE_ACTOR_TOKEN=1`, every request that claims an
actor handle **MUST** include `X-Actor-Token`. Requests without it
are rejected with HTTP 401.

### 4.5 Token scopes

Each token has a `scope` (`admin` | `speak` | `read-only`). Required
scope per operation:

| Endpoint | Required scope |
|---|---|
| `GET /v2/inbox/stream`, `GET /v2/operations/{id}/events`, `GET /v2/operations/discoverable` | `read-only` |
| `POST /v2/operations`, `POST /v2/operations/{id}/events`, `POST /v2/operations/{id}/close`, `POST /v2/actors/{handle}/heartbeat` | `speak` |
| `POST /v2/actors/{handle}/tokens` (issue), `POST .../revoke` | `admin` |

A token whose scope is *below* the required level **MUST** be
rejected with HTTP 403 and `detail: "X-Actor-Token scope=<actual>
insufficient; this endpoint needs scope>=<needed>"`.

The scope hierarchy is total: `admin > speak > read-only`. An admin
scope satisfies any `read-only` or `speak` requirement.

## 5. Tracing

The bridge **MUST** support W3C traceparent propagation
(<https://www.w3.org/TR/trace-context/>). When an inbound request
carries:

```
traceparent: 00-<trace_id>-<span_id>-<flags>
```

…the bridge **MUST**:

1. Preserve the inbound `trace_id` on the response's `traceparent`.
2. Generate a fresh `span_id` (the bridge's span).
3. Make the active `(trace_id, span_id)` available to internal
   logging so log records emitted during the request handler carry
   them.

When no inbound `traceparent` is present, the bridge **MUST** mint
both a `trace_id` and `span_id` and emit them on the response.

For streaming responses (SSE), `traceparent` is set on the
*initial* HTTP response and applies to all events delivered on that
stream. Clients **MUST** capture it once at subscribe time and
propagate to subsequent POSTs in the same logical session — they
**MUST NOT** look for traceparent inside SSE event data frames
(it is not delivered there).

## 6. Core types

### 6.1 OperationPolicy

```json
{
  "close_policy": "opener_unilateral" | "any_participant"
                | "operator_ratifies" | "quorum",
  "join_policy": "open" | "self_or_invite" | "invite_only",
  "context_compaction": "none" | "rolling_summary",
  "max_rounds": int | null,
  "min_ratifiers": int | null,
  "bot_open": bool,
  "bind_remote_task": bool,
  "requires_artifact": bool
}
```

- `close_policy` (default `opener_unilateral`):
  - `opener_unilateral`: only the opener (or system bypass) may close.
  - `any_participant`: any participant may close.
  - `operator_ratifies`: an actor with role `operator` **MUST** post a
    `chat.speech.ratify` event before close is admissible.
  - `quorum`: at least `min_ratifiers` *distinct* actors **MUST** have
    posted `chat.speech.ratify` before close is admissible.
- `join_policy` (default `self_or_invite`):
  - `open`: anyone may post `chat.speech.join`.
  - `self_or_invite`: an actor may self-join freely.
  - `invite_only`: an actor may post `chat.speech.join` only if they
    already hold a participant role on the op (typically from a
    prior `chat.speech.invite` addressed to them).
- `context_compaction` (default `none`):
  - `none`: no automatic compaction.
  - `rolling_summary`: the bridge accepts summary artifacts from a
    designated `@summarizer` agent. (Not enforced in v3.1; the bridge
    persists the field but does not auto-trigger compaction.)
- `max_rounds`: when set, the bridge **MUST** reject any new
  `chat.speech.*` event past the cap with HTTP 400 and error code
  `policy.max_rounds_exhausted`. Lifecycle events
  (`chat.conversation.opened`, `chat.conversation.closed`) do **not**
  count.
- `min_ratifiers`: required when `close_policy=quorum`. **MUST** be a
  positive integer.
- `bot_open` (default `true`): when `false`, the bridge **MUST**
  reject opens from actors of kind `ai`/`service`. (Enforcement is
  optional in v3.1; bridges that don't enforce **MUST** still
  persist the field verbatim.)
- `bind_remote_task` (only meaningful for `kind=task`): when `true`,
  the bridge creates a `RemoteTask` row coupled to the op for the
  full executor lifecycle (claim / heartbeat / evidence / approval /
  complete-or-fail). When `false`, the op is purely conversational
  and **MUST NOT** be coupled to a `RemoteTask`; close admissibility
  is governed solely by `close_policy`. Default depends on the
  caller surface — see §9.1.
- `requires_artifact` (default `false`): when `true`, the bridge
  **MUST** reject `POST /close` with HTTP 400 + code
  `policy.close_needs_artifact` while the op has zero
  `OperationArtifact` rows attached. Orthogonal to `close_policy`
  — both must be satisfied. Pairs with `speech.evidence` carrying
  a `payload.artifact` (§8.1.2) for ops where deliverable
  existence is part of completion criteria.

A bridge **MUST** materialize a normalized policy on every op at
open time. Clients reading the op **MUST** see all eight fields.

### 6.2 ExpectedResponse

```json
{
  "from_actor_handles": ["@bob", "@carol"],
  "kinds": ["answer", "defer"],
  "by_round_seq": 5
}
```

- `from_actor_handles` (required when the field is present): handles
  obligated to reply. The bridge **MUST** normalize unprefixed
  handles by prepending `@`.
- `kinds` (optional): whitelist of acceptable reply speech kinds.
  Special value `"*"` means "any kind". The literal value `"defer"`
  is **always** admissible regardless of whitelist (carve-out for
  the auto-defer sweeper).
- `by_round_seq` (optional): when present, the policy sweeper
  **MUST** auto-emit `chat.speech.defer` on the addressee's behalf
  if op MAX(seq) exceeds this value without a qualifying reply.

If `expected_response` is present on a speech event, clients
**SHOULD** use `from_actor_handles` as the authoritative reply
contract: an agent that is not in this list **MUST NOT** auto-reply.

If `expected_response` is absent, clients fall back to
`addressed_to_actor_ids` (§6.4) for routing.

### 6.3 Operation

```json
{
  "id": "<uuid>",
  "space_id": "chat:<thread-uuid>",
  "kind": "inquiry" | "proposal" | "task" | "general",
  "state": "open" | "claimed" | "executing" | "blocked_approval"
         | "verifying" | "closed",
  "title": "string",
  "intent": "string | null",
  "metadata": {...},
  "policy": OperationPolicy,
  "resolution": "string | null",
  "resolution_summary": "string | null",
  "closed_by_actor_id": "<uuid> | null",
  "created_at": "iso-8601",
  "updated_at": "iso-8601",
  "closed_at": "iso-8601 | null"
}
```

### 6.4 Event

```json
{
  "id": "<uuid>",
  "operation_id": "<uuid>",
  "actor_id": "<uuid>",
  "seq": int,
  "kind": "chat.speech.claim" | "chat.conversation.opened" | ...,
  "payload": { "text": "...", ... },
  "addressed_to_actor_ids": ["<uuid>", ...],
  "private_to_actor_ids": ["<uuid>", ...] | null,
  "replies_to_event_id": "<uuid> | null",
  "expected_response": ExpectedResponse | null,
  "created_at": "iso-8601"
}
```

- `seq`: monotonically increasing per-op. The bridge **MUST**
  guarantee `(operation_id, seq)` is unique.
- `addressed_to_actor_ids`: actor IDs the speaker explicitly
  addressed. Becomes participants on the op (auto-add).
  **Note (rev 8)**: this field is **structural-only** in v3.1 —
  bridges still maintain it for v1/v2 routing compat, but
  reference clients (`agent_loop.py`, `agent.ts`) populate
  responsibility exclusively via `expected_response.from_actor_handles`.
  Speakers SHOULD set `expected_response` for new code. Future
  rev MAY deprecate `addressed_to_*` once all clients have
  migrated.
- `private_to_actor_ids`: when non-null, only actors in this list
  (plus the speaker) **MUST** be able to read this event. SSE
  fan-out and history GET both enforce.
- `replies_to_event_id`: the event this one replies to. Clients
  **SHOULD** populate when responding.
- `expected_response`: the speaker's reply contract for this event
  (§6.2).

### 6.5 SpeechKinds (closed enum at v3.1)

```
claim, question, answer, propose, agree, object, evidence, block,
defer, summarize, react, move_close, ratify, invite, join
```

| Kind | Semantic |
|---|---|
| `claim` | factual assertion (no obligation on others) |
| `question` | request for information; `expected_response` typical |
| `answer` | direct answer to a `question` |
| `propose` | concrete proposal for an action / decision |
| `agree`, `object` | response to a prior speech (target via `replies_to_event_id`) |
| `evidence` | append evidence; typically with an artifact |
| `block` | hard objection: "I will not let this proceed" |
| `defer` | "I cannot answer in the requested form" — auto-emitted by sweeper too |
| `summarize` | summarization checkpoint (typically from `@summarizer`) |
| `react` | low-cost ack ("noted", thumb-up) |
| `move_close` | governance: "I move we close this op" |
| `ratify` | governance: "I ratify the close" — counts toward `quorum` / `operator_ratifies` |
| `invite` | governance: bring an outside handle into the op (sets them as `addressed`) |
| `join` | governance: declare self-membership; gated by `policy.join_policy` |

### 6.6 Actor

```json
{
  "handle": "@alice",
  "kind": "human" | "ai" | "service" | "system",
  "status": "online" | "idle" | "offline",
  "last_seen_at": "iso-8601 | null",
  "capabilities": ["..."]
}
```

The bridge **MUST** auto-provision an actor row the first time a
handle is observed (via `/v2/inbox/stream` subscribe, `POST /events`,
or token issuance).

## 7. Endpoints

This section is normative only for endpoints tagged `protocol-v3-public`
in `/v3/schema/openapi-public`.

### 7.1 Operations

#### `POST /v2/operations` — open an operation

Request:

```json
{
  "space_id": "<discord_thread_id-or-canonical>",
  "kind": "inquiry" | "proposal" | "task",
  "title": "string",
  "intent": "string | null",
  "addressed_to": "@handle | null",
  "opener_actor_handle": "@alice",
  "objective": "string | null",     // required when kind=task
  "success_criteria": {...},        // required when kind=task
  "policy": OperationPolicy | null
}
```

Response: 201 with serialized `Operation` (§6.3).

Errors:

| Status | Reason |
|---|---|
| 400 | invalid policy, missing required field for kind, or unknown enum |
| 401 | bad shared bearer / invalid X-Actor-Token |
| 403 | claimed `opener_actor_handle` doesn't match X-Actor-Token; or scope < `speak` |
| 404 | space not found |

#### `GET /v2/operations/{id}` — read an operation

Response 200: serialized `Operation`.

#### `POST /v2/operations/{id}/events` — append a speech event

Request:

```json
{
  "actor_handle": "@bob",
  "kind": "speech.claim" | "speech.question" | ...,
  "payload": { "text": "...", "..." },
  "addressed_to": "@handle | null",
  "addressed_to_many": ["@handle", ...] | null,
  "replies_to_event_id": "<uuid> | null",
  "private_to_actors": ["@handle", ...] | null,
  "expected_response": ExpectedResponse | null
}
```

Response: 201 with serialized `Event`.

Errors include all policy-engine codes from §13.

#### `POST /v2/operations/{id}/close` — close an operation

Request:

```json
{
  "actor_handle": "@alice",
  "resolution": "answered" | "rejected" | ...,
  "summary": "string | null"
}
```

Response: 200 with serialized `Operation`.

The set of valid `resolution` values is per-`kind` and is a closed
enum (§9.2).

#### `GET /v2/operations/{id}/events`

Read the event log for an op.

Query: `actor_handle=@<handle>` — required for privacy redaction.
Events whose `private_to_actor_ids` excludes the asker (and isn't the
speaker) **MUST** be filtered.

#### `GET /v2/operations/discoverable`

List ops the asker is **not yet a participant of** but could
legitimately join.

Query:

```
for=@<handle>           required — the asking actor
space_id=<id>           optional — scope to one space
kinds=<comma-list>      optional — only return ops whose latest
                                  expected_response.kinds intersects
                                  with the asker's declared kinds
limit=<int>             optional, default 100, max 500
cursor=<opaque>         optional — pagination token from prior page
```

Response:

```json
{
  "actor_handle": "@bob",
  "items": [
    { "id": "...", "kind": "...", "title": "...", "policy": {...},
      "created_at": "..." },
    ...
  ],
  "next_cursor": "<opaque-string> | null"
}
```

### 7.2 Inbox

#### `GET /v2/inbox?actor_handle=@<handle>`

Returns ops the asker participates in (any role). Used for
"what needs my attention" UI; a polling fallback for SSE.

#### `GET /v2/inbox/stream?actor_handle=@<handle>`

Server-Sent Events stream of every op event the asker is permitted
to see. Frame format:

```
event: open
data: {"space_id":"v2:inbox:<actor_id>","actor_id":"<uuid>"}

event: v2.event
data: <InboxEnvelope>

event: heartbeat
data: {}
```

Where `InboxEnvelope` is:

```json
{
  "operation_id": "<uuid>",
  "event_id": "<uuid>",     // = OperationEvent.id
  "seq": int,
  "kind": "chat.speech.X" | "chat.conversation.opened" | ...,
  "actor_id": "<uuid>",
  "payload": { "text": "...", ... },
  "addressed_to_actor_ids": ["<uuid>", ...],
  "private_to_actor_ids": ["<uuid>", ...] | null,
  "replies_to_event_id": "<uuid> | null",
  "expected_response": ExpectedResponse | null,
  "created_at": "iso-8601",
  "cursor": "<opaque>"
}
```

That is, the envelope **MUST** include all `OperationEvent`
fields the asker is permitted to see (subject to privacy
redaction; §10).

**Line termination**: bridges **MUST** terminate SSE lines per the
W3C EventSource spec — any of `\n`, `\r`, or `\r\n`. Clients
**MUST** accept all three terminators.

**SSE comments**: lines beginning with `:` are SSE comments and
**MUST** be ignored by clients. Bridges **MAY** emit them (for
load-balancer keep-alive) and **MUST** tolerate them on
intermediate proxies.

The bridge **MUST** emit heartbeat events at no more than the
configured `heartbeat_seconds` interval (default 15s) so clients can
distinguish a stalled stream from idle.

The asker is auto-provisioned as an actor on first subscribe.

### 7.3 Actor tokens

#### `POST /v2/actors/{handle}/tokens`

Request: `{ "label": "...", "scope": "admin" | "speak" | "read-only" }`

Response 201: `{ id, actor_handle, token, label, scope, created_at }`.
The `token` field is plaintext, returned ONCE.

#### `GET /v2/actors/{handle}/tokens`

Response: `{ tokens: [{ id, label, scope, created_at, revoked_at }, ...] }`.
Plaintext is **NEVER** returned.

#### `POST /v2/actors/{handle}/tokens/{token_id}/revoke`

Soft-revoke; sets `revoked_at`. Subsequent uses of the plaintext
fail with HTTP 401.

#### `POST /v2/actors/{handle}/heartbeat`

Liveness ping. Updates `last_seen_at`. Clients **MUST** send
`Content-Type: application/json` with body `{}` (the empty JSON
object). Bridges **MAY** accept other request body shapes for
robustness but **MUST** accept `{}`.

Response: `{ actor_handle, last_seen_at }`.

### 7.4 Schema discovery

#### `GET /v3/schema/types`

Returns hand-curated JSON Schemas for `OperationPolicy`,
`ExpectedResponse`, `SpeechKinds`, `PolicyErrorCodes`. Public; no
auth required (TODO: this MAY change in a future minor — track
`/v3/schema/openapi-public` for the authoritative public surface).

#### `GET /v3/schema/openapi-public`

Returns the OpenAPI 3.1 doc filtered to the v3 public surface.

## 8. Speech kinds — semantics

(See §6.5 for the closed list at v3.1.)

### 8.1 General response

When an event with `expected_response.from_actor_handles=[X]` is
posted, actor X **SHOULD** respond with one of:

- a speech kind in `expected_response.kinds` (or any kind if `kinds`
  is absent or contains `"*"`),
- a `chat.speech.defer` (universally admissible),
- silence — in which case the policy sweeper auto-emits a defer on
  X's behalf when `by_round_seq` elapses.

### 8.1.1 Continuation — INVITING vs TERMINAL replies

Every reply is either INVITING (it expects a specific actor to act
next) or TERMINAL (it stands on its own). The wire mechanism for
this is `expected_response`:

- **TERMINAL reply**: `expected_response` is absent or `null`.
  No actor is obligated to respond. The reply may still be addressed
  via `addressed_to`/`addressed_to_many` for routing, but no
  obligation is created.

- **INVITING reply**: `expected_response.from_actor_handles` is
  non-empty. The named actors **SHOULD** respond per §8.1.

**Bridges MUST NOT synthesize default `expected_response` values
based on speech kind.** Workflow assumptions ("a propose obligates
peers to vote", "a question obligates the addressee to answer") are
*application-level choices made by the speaker*, expressed by the
speaker setting `expected_response` explicitly. The protocol
transports this contract; it does not infer it.

Reference clients are encouraged to expose a compact reply-prefix
grammar to their backing model (e.g. `[KIND→@a,@b kinds=ratify]
body...`) that translates directly to `expected_response` on the
outgoing event. The grammar is a *client convenience*, not a wire
form. The reference Python (`agent_loop.py`) and TypeScript
(`agent.ts`) parsers cap the prefix at **200 chars (inclusive of
the closing `]`)**. Inputs whose `]` lands at index ≤ 200 are
parsed; longer inputs fall back to a TERMINAL `claim` with the
original text as the body. Both implementations agree at the exact
boundary — see fixture cases `T33` / `T34` in
`tests/fixtures/reply_prefix_cases.json`.

### 8.1.2 Evidence — artifact attachment

`chat.speech.evidence` carries an OPTIONAL `payload.artifact` dict
that formally registers a deliverable produced during the op:

```json
{
  "kind": "speech.evidence",
  "payload": {
    "text": "wrote dodge.html",
    "artifact": {
      "kind": "code" | "screenshot" | "diff" | "log" | "file" | ...,
      "uri": "file:///path/to/dodge.html",
      "sha256": "<64-char hex>",
      "mime": "text/html",
      "size_bytes": 5942,
      "label": "optional human label",
      "metadata": { "any": "extra" }
    }
  }
}
```

Fields:

- `kind` (required): a free-form category. Reference clients use
  `code` / `screenshot` / `diff` / `log` / `file` / `image` /
  `audio` / `evidence`, but the bridge does not enforce a vocab.
- `uri` (required): non-empty string. The bridge does not interpret
  `uri` semantics (could be `file://`, `s3://`, `http://`, …) — it
  stores it verbatim. Consumers fetch + verify against `sha256`.
- `sha256` (required): exactly 64 hex chars. The bridge does not
  recompute; the writer is responsible for matching content.
- `mime` (required): non-empty string. Best-effort hint, not enforced.
- `size_bytes` (required): non-negative int.
- `label` (optional str), `metadata` (optional object).

When all required fields are present and well-formed, the bridge
**MUST** create an `OperationArtifact` row tied to the event id
and surface it via `GET /v2/operations/{id}/artifacts`. When the
field is absent, the event is recorded normally with no artifact.
When the field is *partially* present (at least one of the
artifact keys but missing required fields), the bridge **MUST**
reject with HTTP 400 (`payload.artifact: …`) — silent drop hides
executor bugs.

Other speech kinds **MUST** ignore `payload.artifact`. The field
is recognized only on `speech.evidence` because evidence is the
designated carrier for "a deliverable now exists" — a `claim` or
`propose` carrying a file path is just prose.

Reference Python clients support a convenience header in the
reply body — `ARTIFACT: path=<rel> [kind=<k>] [label=<l>]` on the
first line — which `BridgeAgentLoop` strips, stats under
`agent_cwd`, and converts to a structured `payload.artifact`
before posting. The header is a client-side convenience; non-
Python clients construct the dict directly.

### 8.2 Governance acts

`chat.speech.move_close`, `chat.speech.ratify`, `chat.speech.invite`,
`chat.speech.join` carry governance semantics:

- `move_close`: payload `{ "text": "...", "resolution": "..." }`.
  Informational; the actual close is a separate `POST /close`. Used
  to coordinate humans + agents on intended resolution.
- `ratify`: payload `{ "text": "..." }`. Counts toward `quorum` /
  `operator_ratifies` close policies. The bridge **MUST** de-dup on
  speaker actor (multiple ratifies from one actor count as one).
- `invite`: payload `{ "text": "...", "addressed_to": "@target" }`.
  The bridge auto-adds the target as a participant with role
  `addressed`.
- `join`: payload `{ "text": "..." }`. The speaker becomes a
  participant. Gated by `policy.join_policy`:
  - `open`: always admissible.
  - `self_or_invite`: always admissible (self-join is allowed).
  - `invite_only`: speaker **MUST** already be a participant (e.g.
    from a prior `invite`). HTTP 403 with code
    `policy.join_invite_only` otherwise.

## 9. Operation lifecycle

### 9.1 Choosing `kind`

The op `kind` is the strongest hint a caller gives the bridge about
what shape the work takes. Pick by what `closed` will mean.

| `kind` | When to use | Closure shape | `bind_remote_task` default |
|---|---|---|---|
| `inquiry` | Question, discussion, decision. Result *is* the consensus / answer. | `close_policy` only. | n/a |
| `proposal` | A specific change is on the table; participants ratify or object. | `close_policy` (often `quorum` or `operator_ratifies`). | n/a |
| `task` (collab) | Personas collaborate to *produce* a deliverable in-room. No external executor will `claim` the work. | `close_policy` only — same lifecycle gates as `inquiry`. | `false` (when opened via `/v2/operations`) |
| `task` (executor) | An external agent (`claude-pca`, `remote-codex`, …) will `claim` the work via lease, post `evidence`, and `complete`. | RemoteTask lifecycle drives close (`complete`/`fail`). | `true` (caller opts in via `policy.bind_remote_task: true`) |
| `general` | Always-open thread chat. | Cannot close. | n/a |

`kind=task` covers two distinct shapes and the choice changes
admissibility. With `bind_remote_task=true` the bridge couples the
op to a `RemoteTask`, and a manual `POST /close` is rejected with
`policy.task_bound_active` while that task is non-terminal — the
executor must `complete` or `fail` instead. With `bind_remote_task=
false` the op behaves like an `inquiry` for closure purposes:
ratifiers + `close_policy` decide.

The `/v2/operations` HTTP endpoint **MUST** default
`bind_remote_task=false` for `kind=task` opens that omit the field
(the v3 collab default). Internal `/api/chat` v1 callers preserve
the legacy default `true`. Either default may be overridden
explicitly by the caller.

### 9.2 State machine

| State | Transitions |
|---|---|
| `open` | → `claimed` (kind=task only); → `closed` |
| `claimed` | → `executing`, → `open`, → `closed` |
| `executing` | → `blocked_approval`, → `verifying`, → `claimed`, → `closed` |
| `blocked_approval` | → `executing`, → `claimed`, → `closed` |
| `verifying` | → `executing`, → `closed` |
| `closed` | terminal |

For `kind=inquiry` and `kind=proposal`, only `open ↔ closed` matter.
The intermediate states are reserved for `kind=task` lifecycle.

### 9.3 Resolutions per kind

| Kind | Allowed resolutions |
|---|---|
| `inquiry` | `answered`, `dropped`, `escalated`, `abandoned` |
| `proposal` | `accepted`, `rejected`, `withdrawn`, `superseded`, `abandoned` |
| `task` | `completed`, `failed`, `cancelled`, `abandoned` |
| `general` | (cannot close) |

The bridge **MUST** reject `POST /close` with HTTP 400 if the
`resolution` is not in the kind's allowed set, except when the
caller is system (`bypass_task_guard=true`) — system closes may use
`abandoned` regardless.

### 9.4 Close gate (policy enforcement)

After capability check passes, the bridge **MUST** consult
`policy.close_policy`:

| Policy | Additional requirement |
|---|---|
| `opener_unilateral` | none (capability gate is sufficient) |
| `any_participant` | closer **MUST** be a participant |
| `operator_ratifies` | at least one participant with role `operator` **MUST** have posted `chat.speech.ratify` on this op |
| `quorum` | at least `min_ratifiers` *distinct* actors **MUST** have posted `chat.speech.ratify` |

When `kind=task` and `policy.bind_remote_task=true`, an additional
gate applies: the close path **MUST** reject manual `POST /close`
calls while the bound `RemoteTask` is in a non-terminal state
(`queued` / `claimed` / `executing` / `verifying` /
`blocked_approval`). The executor must terminate the task via
`/complete` or `/fail` (which run with `bypass_task_guard=true`)
before the op can close. This guard does **NOT** apply when
`bind_remote_task=false`; see §9.1.

When `policy.requires_artifact=true`, the bridge **MUST** reject
`POST /close` with HTTP 400 + code `policy.close_needs_artifact`
while the op has zero `OperationArtifact` rows attached. This
gate is orthogonal to `close_policy` — both must be satisfied.
Artifacts arrive via `speech.evidence` (§8.1.2).

Failures map to error codes in §13.

## 10. Privacy

### 10.1 `private_to_actors`

A speech event with `private_to_actors=["@bob"]` is visible only to:

- the speaker
- actors in the list

Non-recipients **MUST NOT** receive the event via SSE fan-out and
**MUST NOT** see it in `GET /events?actor_handle=...`. Bridges
**MUST** redact at both surfaces.

### 10.2 Late-join privacy

When a new actor joins an op (via `chat.speech.join`,
`chat.speech.invite`, or first address), they **MUST** see the
public event log up to but not including private events posted
before they joined. The redaction is applied at GET time based on
the requesting actor's identity, not at event-write time.

### 10.3 SSE replay

The bridge **MUST NOT** replay events that pre-date the SSE
subscription window. New SSE subscribers receive only events
broker-published after subscribe. Catch-up is via REST GET.

## 11. Discovery & membership

### 11.1 Membership entry points

An actor becomes a participant of an op via any of:

1. Being the opener of the op (`opener_actor`).
2. Being addressed (`addressed_to` or `addressed_to_many`) by any
   speech event on the op. Auto-adds with role `addressed`.
3. Speaking on the op. Auto-adds with role `speaker`.
4. Posting `chat.speech.join` (gated by `policy.join_policy`).
5. Receiving a `chat.speech.invite` from an existing participant.

### 11.2 Discovery

`GET /v2/operations/discoverable?for=@<handle>` returns ops the
asker:

- is **not** a participant of, and
- **could** legitimately join under the op's `policy.join_policy`.

For `invite_only` ops, the bridge **MUST** filter ops where the
asker isn't already in the participant list (i.e. wasn't invited).

### 11.3 The `kinds` filter

When the discovery query supplies `kinds=<list>`, the bridge **MUST**
inspect the most recent (last 20) events of each candidate op for an
`expected_response.kinds` whitelist. If found, and the op's
whitelist doesn't intersect with the asker's declared kinds (and
doesn't contain `"*"`), the op **MUST** be filtered out. This is a
best-effort relevance filter, not a hard authorization gate.

## 12. Policy enforcement

The bridge **MUST** enforce, at write time:

### 12.1 `max_rounds`

When `op.policy.max_rounds` is set, count `chat.speech.*` events on
the op. If `count >= max_rounds`, reject the new event with HTTP
400 and code `policy.max_rounds_exhausted`. Lifecycle events do not
count toward the cap.

### 12.2 Reply-kind whitelist

When the proposed event's `replies_to_event_id` points at an event
whose `expected_response.kinds` is set:

- If `kinds` contains `"*"`: any kind admissible.
- Else if the proposed event's kind is one of the **universal
  carve-outs** (`defer`, `evidence`, `object`): admissible
  regardless of the whitelist (rev 8).
- Else if the proposed event's kind is in `kinds`: admissible.
- Else: reject with HTTP 400 and code `policy.reply_kind_rejected`.

The carve-outs exist because the whitelist is meant to shape
*voting* moments, not block legitimate moves a speaker should
always be able to make:

- `defer` — "I cannot answer in the requested form". Required for
  the auto-defer sweeper (§12.3) to work under any whitelist.
- `evidence` — deliverable carrier (§8.1.2). Forbidding evidence
  would mean a [PROPOSE kinds=ratify,object] could not be answered
  with a patched file when the patch is exactly what the propose
  expects. Demand-patch loops would deadlock.
- `object` — late-arriving counter-evidence is always valid. A
  poorly narrowed whitelist must not silently convert a real
  disagreement into rejection.

Bridge implementations MUST honour these three carve-outs.
Reference clients (`agent_loop.py`) surface the whitelist + any
HTTP 400 rejection back to the agent's brain so it can self-correct
within the contract.

### 12.3 Auto-defer sweeper

A bridge **MAY** run a periodic sweeper (default 30s) that scans
open ops for events with `expected_response.by_round_seq` elapsed:

- For each addressee in `expected_response.from_actor_handles` who
  has not yet replied with `replies_to_event_id` pointing at the
  trigger:
- Emit `chat.speech.defer` on the addressee's behalf, with
  `replies_to_event_id` set to the trigger.

A bridge that does not run the sweeper **MUST** still accept manual
defers; the sweeper is purely a convenience.

### 12.4 Close policy

See §9.3.

## 13. Error codes

The bridge **MUST** return error responses in this shape:

```json
{ "detail": "human-readable message" }
```

For policy-engine-level rejections, `detail` **MUST** start with
the prefix `policy: ` so clients can branch on the prefix without
parsing the message. The specific rejection is identified by one of
the codes below. The HTTP status is normative — clients **MUST**
branch on (status, code) pairs:

| Code | When |
|---|---|
| `policy.max_rounds_exhausted` | Speech event would exceed `op.policy.max_rounds` |
| `policy.reply_kind_rejected` | Speech kind not in trigger's `expected_response.kinds` |
| `policy.close_needs_operator_ratify` | Close requires `operator_ratifies` and no operator has ratified |
| `policy.close_needs_quorum` | Close requires `quorum` and `min_ratifiers` not yet met |
| `policy.close_needs_participant` | `close_policy=any_participant` and closer is not a participant |
| `policy.join_invite_only` | `join_policy=invite_only` and joiner has no prior participant role |
| `policy.invite_needs_participant` | `chat.speech.invite` from non-participant |
| `policy.close_needs_artifact` | `requires_artifact=true` and op has zero `OperationArtifact` rows |

Perimeter-bound rejections from the adversarial-robustness layer
(see §17 for caps and rationale):

| Code | HTTP | When |
|---|---|---|
| `body.too_large` | 413 | Request body exceeds `BRIDGE_MAX_BODY_BYTES` (default 1 MiB) |
| `request.timeout` | 504 | Handler runtime exceeded `BRIDGE_REQUEST_TIMEOUT_S` (default 30 s) |
| `payload.depth_exceeded` | 400 | Free-form JSON nesting exceeds `BRIDGE_MAX_JSON_DEPTH` (default 32) |

Authentication / scope rejections use the corresponding HTTP status:

| Status | When |
|---|---|
| 401 (`X-Actor-Token is invalid or revoked`) | Token absent (in strict mode), bad, or revoked |
| 403 (`X-Actor-Token is bound to ...`) | Token's bound handle ≠ claimed handle |
| 403 (`X-Actor-Token scope=...`) | Token scope insufficient for endpoint |

## 14. Observability

### 14.1 Diagnostics endpoint

`GET /v2/diagnostics` returns:

```json
{
  "broker": {...},
  "agents": [...],
  "operations": { "by_state": {...}, "by_kind": {...} },
  "actors": { "total": int, "presence": [{ handle, status, last_seen_at }] },
  "protocol_versions": { "3.0": int, "3.1": int }
}
```

The `protocol_versions` counter is the authoritative signal for
deciding when a minor version may be retired.

### 14.2 Tracing

See §5. Bridges **MUST** emit at least one log record per request
that carries the active `trace_id` and `span_id` so log consumers
can correlate without joining HTTP access logs.

## 15. Reserved / out-of-spec

These exist in code but are **NOT** part of v3 normative behavior:

- The v1 chat surface (`/api/chat/...`, `/api/remote-claude/...`,
  etc.). These are internal in v3 and **MUST NOT** be relied on by
  v3 clients. The full FastAPI OpenAPI at `/openapi.json` covers
  them but is not stable.
- The `agent_service` / in-process agent runner — retired in v3.
- The `BRIDGE_AGENT_BROADCAST` and `BRIDGE_AGENT_MAX_PER_OP` env
  vars on the agent runtime — retired in phase 3 cleanup.

## 16. Conformance

A v3 implementation is conformant if it passes the conformance test
pack at [tests/conformance/](../tests/conformance/). The pack is
HTTP-only and implementation-agnostic; running it against a foreign
server is the canonical conformance check.

The pack covers 32 required behaviors at v3.1:

| Area | Spec §  |
|---|---|
| Schema discovery | §2, §6.5, §13 |
| Version negotiation | §3 |
| Traceparent propagation | §5 |
| Per-actor token issue/binding/revoke + scopes | §4 |
| Policy enforcement (max_rounds / kind whitelist / close / join) | §12 |
| Discovery + heartbeat | §7.1, §7.3 |
| Lifecycle (event log, reply chain, privacy) | §6.4, §10 |

A *partial* implementation **MAY** ship without the policy sweeper
(§12.3) and without `context_compaction=rolling_summary`
enforcement, but **MUST** still accept the corresponding fields and
persist them verbatim. The conformance pack asserts on eventual
state, not exact sweeper cadence — so a slower sweeper still passes.

To run the pack against a live impl:

```
BRIDGE_TEST_MODE=1 <start your bridge>
BRIDGE_CONFORMANCE_BASE_URL=http://your-bridge:port \
BRIDGE_SHARED_AUTH_TOKEN=<token> \
python -m pytest tests/conformance/ -q
```

See [tests/conformance/README.md](../tests/conformance/README.md)
for details.

## 17. Bounds & quotas

The bridge **MUST** apply transport-level and parser-level bounds at
its perimeter. Without them the kernel's typed-vocabulary guarantees
do not survive contact with adversarial input. These bounds are a
*single boundary* — clients see them as HTTP rejections; downstream
kernel code can assume input is bounded.

### 17.1 Caps

| Setting | Default | Env var | Behavior |
|---|---|---|---|
| Max request body | 1 MiB | `BRIDGE_MAX_BODY_BYTES` | Bodies above the cap are rejected with 413 + code `body.too_large`. Content-Length is checked before reading; chunked uploads are counted as bytes accumulate. |
| Max JSON nesting | 32 levels | `BRIDGE_MAX_JSON_DEPTH` | Free-form fields (`policy`, `metadata`, `payload`, `expected_response`, `success_criteria`) walked at POST entry; over-cap rejected with 400 + code `payload.depth_exceeded`. |
| Max handler runtime | 30 s | `BRIDGE_REQUEST_TIMEOUT_S` | Handler wrapped in `asyncio.wait_for`; over-cap returns 504 + code `request.timeout`. SSE / heartbeat / health prefixes are exempt. |
| Max regex input length | 8 KiB | (not configurable) | Adapter regexes (`@<handle>`, `T-NNN`, `OPS:`, handoff/discuss markers) reject inputs above the cap before invoking the regex. ReDoS defense; longer messages bypass adapter parsing without affecting kernel ingest. |

### 17.2 Rollout mode

`BRIDGE_BOUNDS_LOG_ONLY=1` makes body/timeout/depth violations log a
warning and pass through instead of returning 4xx/5xx. Intended for
staged rollout: operators ship the bounds in observe-only mode for ≥1
sweep, then flip to enforcement once the cap-hit rate is acceptable.
This mirrors the phase-10 "surface first, enforce next" sequence.

### 17.3 SSE & long-poll exemption

Streaming endpoints **MUST** be added to the timeout-exempt prefix
list. Today: `/v2/inbox/`, `/v2/operations/.*/seen`, `/health`. New
streaming endpoints introduced in future versions **MUST** update
this list (otherwise the timeout middleware will close the connection
after `BRIDGE_REQUEST_TIMEOUT_S`).

### 17.4 What is NOT covered

- Idempotency keys on mutating endpoints (axis J — separate concern)
- Distributed rate limiting across instances (infra layer, not in spec)
- GZIP decompression-bomb defense (no gzip middleware shipped today)
- TLS / connection-level limits (operator deployment concern)

These are intentionally out of scope for §17. A future revision may
introduce idempotency-key as a normative requirement; until then,
clients **SHOULD** treat retries as at-least-once.

## 18. Changelog

| Rev | Date | Notes |
|---|---|---|
| 1 | 2026-05-03 | Initial normative document (v3.1) |
| 2 | 2026-05-03 | Patches from interop sprint with TS reference client. Clarified: timestamp precision (§1), per-actor token verbatim (§3.4), traceparent on streaming responses (§5), inbox envelope shape + SSE line-termination + comment-line tolerance (§7.2), heartbeat body shape (§7.3), error response shape (§13). See [protocol-v3-interop-findings.md](./protocol-v3-interop-findings.md). |
| 3 | 2026-05-03 | Phase 6: §8.1.1 added — INVITING vs TERMINAL reply distinction. Bridges **MUST NOT** synthesize default `expected_response` from speech kind; workflow shape is a speaker-level choice. The protocol stays workflow-agnostic; clients use a compact prefix grammar (`[KIND→@a,@b kinds=…] body`) at their convenience. See `tests/test_v3_next_responder_parser.py` for the grammar. |
| 4 | 2026-05-03 | T1.1: `policy.bind_remote_task` field added to `OperationPolicy` (§6.1). New §9.1 "Choosing `kind`" makes the collab-task vs executor-task distinction normative. `/v2/operations` defaults `bind_remote_task=false` for `kind=task`; the v1 `/api/chat` path keeps the legacy `true` default. §9.4 documents the task-bound close guard (only fires when `bind_remote_task=true`). Sub-section 9.2/9.3 renumbered. Tests: `tests/test_v3_bind_remote_task_policy.py`. |
| 5 | 2026-05-03 | Cross-impl parser conformance: 37-case JSON fixture at `tests/fixtures/reply_prefix_cases.json` exercised by Python (`tests/test_v3_parser_cross_impl_fixture.py`) and TypeScript (`clients/ts-agent-loop/src/parser-fixture.test.ts`). Fixed Python parser drift on prefix-length boundary — `]` at exact idx 200 was rejected by Python but accepted by TS. Both impls now agree on **≤ 200 chars (inclusive)**, documented in §8.1.1. |
| 6 | 2026-05-03 | T1.2: artifact-on-evidence (§8.1.2). `speech.evidence` may carry a `payload.artifact` dict (`kind/uri/sha256/mime/size_bytes` + optional `label`/`metadata`). The bridge auto-creates an `OperationArtifact` row tied to the event. Other speech kinds ignore the field. Partial/malformed artifact is rejected with HTTP 400 (no silent drop). Reference Python parser (`agent_loop.py`) understands an `ARTIFACT: path=...` first-line header and stats the file before posting. Tests: `tests/test_v3_artifact_evidence.py` (HTTP path) + `tests/test_v3_agent_loop_artifact_extract.py` (parser). |
| 7 | 2026-05-03 | T2.1: `policy.requires_artifact` field added to `OperationPolicy` (§6.1, §9.4). When `true`, close is rejected with HTTP 400 + code `policy.close_needs_artifact` until ≥1 `OperationArtifact` row is attached. Orthogonal to `close_policy` — both must be satisfied. Default `false` (back-compat). Pairs with T1.2 evidence-with-artifact for "no close without deliverable" semantics. Tests: `tests/test_v3_requires_artifact_policy.py`. |
| 8 | 2026-05-04 | RPG smoke 결함 정리 — `evidence` 와 `object` 가 `defer` 옆에 universal carve-out 으로 추가됨 (§12.2). 좁은 `kinds=` whitelist 가 demand-patch 흐름을 묶던 deadlock 해소. Reference `agent_loop.py` 가 (a) HTTP 400 rejection 을 다음 prompt 에 노출 (D2) 하고 (b) 트리거의 `expected_response.kinds` 를 LLM 에게 미리 알려줌 (D8) — 자기교정 가능. `addressed_to_*` 가 v3.1 에선 structural-only / `expected_response` 가 권장 surface 임을 §6.4 에 명시. Tests: `tests/test_v3_reply_kind_carveouts.py`, `tests/test_v3_agent_loop_rejection_surface.py`. |
| 9 | 2026-05-04 | Unity arcade smoke 후속 — 6 결함 일괄 fix. **D9** ratify intent split: quorum gate 가 close-intent ratify 만 카운트 (explicit `payload.intent="close"` / replies_to `move_close` / replies_to artifact-bearing event / op 이미 artifact 보유). **D10** agent_loop prompt 에 "[KIND] MUST be position-0" 강조. **D11** `payload.artifacts: list` 다중 artifact 첨부 (singular `payload.artifact` 보존). **D14** agent_loop 가 claude run "no result" 도 `_last_run_failure` 로 캡처 → 다음 prompt 에 ⚠️ 노출 (D2 패턴). **P9.5** Discord forwarder 가 `/operations` open + `/close` lifecycle marker 도 forwarding. **D3** `expected_response.from_actor_handles` 에 미존재 handle WARN log (옵션 `BRIDGE_REQUIRE_KNOWN_HANDLES=1` 시 reject 400). 페르소나 prompt 에 domain pre-flight checklist 추가 (D12). Tests: `test_v3_ratify_intent_split.py`, `test_v3_multi_artifact.py`, `test_v3_discord_lifecycle_forward.py`, `test_v3_handle_validation.py`. |
| 11 | 2026-05-04 | Phase 11 — adversarial-robustness perimeter (axis H). New §17 "Bounds & quotas" defines transport-level (body 1 MiB, timeout 30 s) + parser-level (JSON depth 32, regex input 8 KiB) caps. New error codes `body.too_large` (413), `request.timeout` (504), `payload.depth_exceeded` (400) added to §13. `BRIDGE_BOUNDS_LOG_ONLY=1` enables observe-only rollout. SSE/heartbeat/health prefixes exempt from handler timeout. Tests: `test_security_bounds.py`, `test_security_regex_safety.py`, `tests/conformance/test_adversarial.py`. |
| 10 | 2026-05-04 | Phase 10 — silent failures 전부 surface. **P10.1+P10.5** agent_loop pre-flight 가 본문 가운데 `[KIND]` 발견 시 post 거부 + self-rejection 캡처 (D10 깊이 패치). **P10.2** ARTIFACT 헤더가 path stat 실패 시 `_last_artifact_failure` 캡처 → 다음 prompt ⚠️. **P10.3** 5xx 도 `_last_post_rejection` 에 캡처 (transient flag) — 4xx 와 별도 가이드. **P10.4** `policy.requires_artifact` gate 가 success resolution (`completed`/`accepted`/`answered` 등) 만 적용 — abandoned/cancelled/failed/withdrawn/superseded/dropped 는 bypass. **P10.6** 같은 op 같은 kind 연속 4xx 시 prompt escalate (🚨 "Stop trying [<kind>]"). **P10.7** TS reference (`agent.ts`) 가 plural artifacts + intent 패스스루. **P10.10** task-bound close 메시지에 `bind_remote_task=false` 우회 hint. Tests: `test_v3_phase10_silent_failure_surfacing.py`, `test_v3_abandoned_bypass_artifact.py`. 회귀: `test_kernel_v3_*` / conformance / multiturn helper 들에 close-intent stamping 추가. |

## Appendix A — Error code catalog (machine-readable)

```json
{
  "policy.max_rounds_exhausted": {
    "http_status": 400,
    "summary": "speech.* event would exceed op.policy.max_rounds"
  },
  "policy.reply_kind_rejected": {
    "http_status": 400,
    "summary": "reply kind not in trigger.expected_response.kinds"
  },
  "policy.close_needs_operator_ratify": {
    "http_status": 400,
    "summary": "close_policy=operator_ratifies requires a ratify from an operator-role participant"
  },
  "policy.close_needs_quorum": {
    "http_status": 400,
    "summary": "close_policy=quorum requires min_ratifiers distinct ratifiers"
  },
  "body.too_large": {
    "http_status": 413,
    "summary": "request body exceeds the configured maximum (BRIDGE_MAX_BODY_BYTES)"
  },
  "request.timeout": {
    "http_status": 504,
    "summary": "handler runtime exceeded the configured ceiling (BRIDGE_REQUEST_TIMEOUT_S)"
  },
  "payload.depth_exceeded": {
    "http_status": 400,
    "summary": "free-form JSON nesting exceeds BRIDGE_MAX_JSON_DEPTH"
  },
  "policy.close_needs_participant": {
    "http_status": 400,
    "summary": "close_policy=any_participant requires the closer to be a participant"
  },
  "policy.join_invite_only": {
    "http_status": 400,
    "summary": "join_policy=invite_only requires a prior invite"
  },
  "policy.invite_needs_participant": {
    "http_status": 400,
    "summary": "speech.invite must come from an existing participant"
  },
  "policy.close_needs_artifact": {
    "http_status": 400,
    "summary": "policy.requires_artifact=true and op has zero OperationArtifact rows; post a speech.evidence with payload.artifact first"
  }
}
```

## Appendix B — Backward compatibility matrix

| Field / behaviour | v3.0 | v3.1 |
|---|---|---|
| `OperationPolicy` field set | unchanged | unchanged |
| Speech kinds | base 11 | + `move_close`, `ratify`, `invite`, `join` |
| `traceparent` | not required | accepted; bridge always echoes |
| `X-Protocol-Version` header | not required | accepted; bridge always echoes supported list |
| `BRIDGE_REQUIRE_PROTOCOL_VERSION` strict mode | not available | available |
| `X-Actor-Token` per-actor token | not required | accepted; gates handle binding |
| `BRIDGE_REQUIRE_ACTOR_TOKEN` strict mode | not available | available |
| Token scopes | not present | `admin`/`speak`/`read-only` |
| `/v2/operations/discoverable` `kinds` filter | absent | present |
| `/v2/operations/discoverable` `cursor` pagination | absent | present |
| `/v2/actors/{h}/heartbeat` | absent | present |
| `/v2/diagnostics.protocol_versions` | absent | present |
| Auto-defer sweeper | absent | present (default on, opt-out via env) |

A v3.0 client **MUST** continue to function against a v3.1 bridge
without any modification, modulo features it didn't ask for.
