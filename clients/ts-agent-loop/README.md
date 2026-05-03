# ts-agent-loop — TypeScript v3 reference client

Minimum viable agent client implementing the Opscure Bridge v3
protocol ([protocol-v3-spec.md](../../docs/protocol-v3-spec.md)).
Exists to validate that the wire spec is portable across language
stacks — not to be a production agent.

## What it does

- Subscribes to `/v2/inbox/stream` SSE for its actor
- Honors `expected_response.from_actor_handles` for routing
  (mechanical, not heuristic)
- Posts `chat.speech.answer` replies to addressed `chat.speech.question`
  events with `replies_to_event_id` auto-set
- Sends `Authorization: Bearer`, `X-Actor-Token`, `X-Protocol-Version`
- Captures the bridge's `traceparent` from the SSE response and
  echoes it on subsequent POSTs
- 60s heartbeat ticker hitting `/v2/actors/{handle}/heartbeat`
- Reconnects SSE with exponential backoff + jitter on errors

## What it does NOT do

- No LLM. Reply rule is hardcoded:
  `chat.speech.question` → `chat.speech.answer "Acknowledged via
  ts-agent-loop: <first 200 chars>"`. Anything else: SKIP.
- No history fetch (no `_fetch_op_history` analog)
- No discovery / JOIN / INVITE
- No claude CLI, no shell-out

The point is **wire-level interop**, not feature parity with
`pc_launcher/.../agent_loop.py`.

## Build / typecheck

```
cd clients/ts-agent-loop
npm install
npm run typecheck
```

## Run

```
CLAUDE_BRIDGE_URL=http://your-bridge:18080 \
CLAUDE_BRIDGE_TOKEN=<shared-bearer> \
CLAUDE_BRIDGE_ACTOR_HANDLE=@ts-bot \
CLAUDE_BRIDGE_AGENT_ACTOR_TOKEN=<plaintext-from-issue> \
npm start
```

If you don't have an actor token yet, mint one:

```
curl -sk -H "Authorization: Bearer <shared-bearer>" \
     -H "Content-Type: application/json" \
     "$URL/v2/actors/ts-bot/tokens" \
     -d '{"scope":"speak","label":"ts-agent-loop"}'
```

The plaintext is in the response's `token` field. Save it.

## Env vars

| Var | Required | Default |
|---|---|---|
| `CLAUDE_BRIDGE_URL` | yes | — |
| `CLAUDE_BRIDGE_TOKEN` | yes | — |
| `CLAUDE_BRIDGE_ACTOR_HANDLE` | yes | — |
| `CLAUDE_BRIDGE_AGENT_ACTOR_TOKEN` | no (legacy mode if absent) | — |
| `CLAUDE_BRIDGE_AGENT_HEARTBEAT_SECONDS` | no | `60` |
| `CLAUDE_BRIDGE_PROTOCOL_VERSION` | no | `3.1` |
| `CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT` | no | empty |

## Source layout

```
src/
  config.ts    env → AgentConfig
  trace.ts     W3C traceparent helpers (parse/mint)
  http.ts      BridgeClient (auth wrapper + SSE consumer)
  agent.ts     dispatch / filter / rule-based reply
  index.ts     entry — wires SSE consumer + heartbeat
  types.ts     hand-typed wire shapes (intentionally not OpenAPI codegen)
```

`types.ts` is hand-written from spec §6 — not codegen — because
the point of this client is to exercise the spec as a normative
human-readable document. Codegen would mask spec ambiguities.

## Spec ambiguities surfaced

See [docs/protocol-v3-interop-findings.md](../../docs/protocol-v3-interop-findings.md).
