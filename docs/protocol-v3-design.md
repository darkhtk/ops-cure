# Protocol v3 — Speech Act Pragmatics + Op Governance

Two new primitives close the protocol-level problems surfaced in the
3-persona / 5-task live exercise. v3 is **additive**: phase 1 stores +
threads the new fields, phase 2 enforces them, phase 3 deprecates the
heuristic shims (BROADCAST flag, per-op cap, etc.) they replace.

## Why v3

v2 events were **content-only**. The "who is expected to respond, with
what speech kind, by when" — the *pragmatics* of a speech act — were
either heuristic strings (`addressed_to`) or absent (`replies_to_event_id`
existed but was unused). Op was just an event log, not a governed
process. The result was 20 protocol-level problems clustered around two
root causes (see [protocol-personas-live-log.md](./protocol-personas-live-log.md)
"프로토콜 문제점").

v3 adds two first-class constructs that fix both clusters:

1. **`expected_response`** on every event — the speaker's reply contract.
2. **`policy`** on every op — the governance rules.

## Primitive 1 — `expected_response`

Every speech event may carry an explicit reply contract:

```json
{
  "kind": "chat.speech.question",
  "payload": {"text": "EU latency cause?"},
  "addressed_to": "@investigator",
  "expected_response": {
    "from_actor_handles": ["@investigator"],
    "kinds": ["answer", "defer"],
    "by_round_seq": 5
  }
}
```

| Field | Meaning |
|---|---|
| `from_actor_handles` | who is expected to reply (the rest may, but aren't obligated) |
| `kinds` | restricted set of speech kinds the reply may take. `"*"` = any |
| `by_round_seq` | if no qualifying reply by this op-event seq, op enters pending-defer |

**`expected_response = null` means broadcast-no-reply** — pure announcement, no
one is obligated. This is the missing distinction that v2 lacked.

**Cascade prevention is mechanical now.** Agent reply logic collapses to:

```python
if ev.expected_response and self.actor_handle in ev.expected_response.from_actor_handles:
    respond
elif ev.expected_response is None and ev.addressed_to_actor_ids includes me:
    respond
else:
    skip
```

No `BROADCAST=true` flag. No per-persona `max_per_op` cap. No bot-to-bot ping-pong.

## Primitive 2 — Operation `policy`

Every op carries a normalized governance policy on its metadata:

```json
{
  "policy": {
    "close_policy": "operator_ratifies",
    "join_policy": "self_or_invite",
    "context_compaction": "rolling_summary",
    "max_rounds": 10,
    "min_ratifiers": null,
    "bot_open": true
  }
}
```

### Field reference

| Field | Values | Effect (phase 2 enforcement) |
|---|---|---|
| `close_policy` | `opener_unilateral` (default) / `any_participant` / `quorum` / `operator_ratifies` | Who can close & how |
| `join_policy` | `invite_only` / `self_or_invite` (default) / `open` | Who can JOIN as participant |
| `context_compaction` | `none` (default) / `rolling_summary` | Bridge auto-summary cadence |
| `max_rounds` | int / null | Op-level cap on event count |
| `min_ratifiers` | int / null | Required RATIFY count when `close_policy=quorum` |
| `bot_open` | bool (default true) | Whether non-human openers may open this op kind |

### Defaults

`kernel.v2.contract.DEFAULT_OPERATION_POLICY` — chosen so ops opened
without a policy match v2 behavior exactly.

## Phase 1 — additive (DONE)

What's implemented:

| Component | Change |
|---|---|
| `kernel.v2.contract` | `validate_expected_response`, `validate_operation_policy`, `DEFAULT_OPERATION_POLICY`, all close-policy / join-policy / compaction enums |
| `kernel.v2.repository` | `operation_policy()`, `set_operation_policy()`, `event_expected_response()` extractors |
| `behaviors.chat.conversation_schemas` | `policy` on `ConversationOpenRequest`, `expected_response` + `replies_to_v2_event_id` on `SpeechActSubmitRequest` |
| `behaviors.chat.conversation_service` | Validates and persists policy at open; threads expected_response + v2 reply id through the mirror |
| `kernel.v2.operation_mirror` | `mirror_message` accepts `expected_response`, nests it under `payload._meta.expected_response` (no schema migration needed) |
| `behaviors.chat._publish_v2_inbox_fanout` | Wraps `expected_response` into the SSE-bound envelope |
| `api.v2_operations` | `POST /v2/operations` accepts `policy`; `POST /v2/operations/{id}/events` accepts `expected_response` and (real-time) `replies_to_event_id`; serialized op now exposes `policy`; serialized event now exposes `expected_response` |
| `api.v2_inbox` | SSE stream payload carries `expected_response` |
| `pc_launcher / agent_loop` | Honors `expected_response.from_actor_handles`; auto-sets `replies_to_event_id` on outgoing claims |
| `tests/test_kernel_v3_speech_act_primitives.py` | 7 tests covering policy round-trip, expected_response storage + handle normalization, and real-time reply chain |

Storage uses existing JSON columns (`operations_v2.metadata_json`,
`operation_events_v2.payload_json`) so phase 1 ships **without a database
migration**. Phase 2 may promote the fields to dedicated columns once
indexes are useful.

Compatibility: all fields optional. Old clients ignore them; old data
gets `DEFAULT_OPERATION_POLICY` materialized at open time.

Test status: **478/479 pass** (1 skipped = live LLM). Phase 1 introduces
zero regressions vs the prior 471/472 baseline.

## Phase 2 — opt-in enforcement (TODO)

What turns on:

1. **Cascade prevention end-to-end.** Update agent_loop to drop the
   legacy `BROADCAST` env (still present as a fallback in phase 1).
   When `expected_response` is null on an event, the agent will *no
   longer auto-respond* even with `BROADCAST=true` — broadcast becomes
   "speak when explicitly invited" only.

2. **Close policy enforcement.** `POST /v2/operations/{id}/close`
   currently allows anyone with `CAP_CONVERSATION_CLOSE` to close. With
   `close_policy=operator_ratifies`, the bridge needs to:
   - Reject direct close calls
   - Accept a `MOVE_CLOSE` speech event from any participant
   - Accept `RATIFY` events; transition state to `closed` only when a
     participant with `@operator` role has ratified

3. **`max_rounds` enforcement.** Bridge rejects events past the cap;
   op auto-transitions to `expired`. Replaces the per-persona client
   cap.

4. **Context compaction.** When `context_compaction=rolling_summary`,
   the bridge generates an op summary every N events and stores it as
   an artifact. agent_loop's `_fetch_op_history` swaps to "summary +
   recent N" instead of raw history.

5. **`expected_response.kinds` whitelist enforcement.** Bridge rejects
   replies whose kind isn't in the allowed list. Today an `[AGREE]`
   prefix that disagrees just sails through.

6. **`by_round_seq` expiry.** Background sweep auto-emits a
   `speech.defer` from the addressee when the round budget elapses.
   Other participants then know the addressee couldn't / wouldn't
   answer in time.

## Phase 3 — deprecate the shims (TODO)

Once phase 2 ships:

- Remove `CLAUDE_BRIDGE_AGENT_BROADCAST` env (replaced by `expected_response`)
- Remove `CLAUDE_BRIDGE_AGENT_MAX_PER_OP` env (replaced by `policy.max_rounds`)
- Remove `addressed_to_many` thinking — the canonical "who must respond"
  is `expected_response.from_actor_handles`. `addressed_to_many` stays
  as a label for UI rendering only.
- Remove the post-stamp fallback for `replies_to_event_id` in
  `api/v2_operations.py` — every v3 caller should pass `replies_to_v2_event_id`
  through the submit path.

## Mapping back to the 20-problem list

| Cluster | Problem | Resolution path |
|---|---|---|
| A1 cascade | "Broadcast cascade" | Phase 1 plumbing + Phase 2 enforcement of `expected_response` |
| A2 addressed semantics | empty `addressed_to` ambiguity | `expected_response = null` is now the formal "no responder" signal |
| A4 replies dead | `replies_to_event_id` unused | Phase 1 wire-up: agent_loop sets it; submit path threads it pre-fanout |
| A9 self-loop | "should I respond?" heuristic | Phase 1 mechanical check on `expected_response.from_actor_handles` |
| A14 kind ↔ body | label not enforced | Phase 2 will validate against `expected_response.kinds` |
| B3 self-join | no JOIN mechanism | Phase 2: `JOIN` speech kind + `policy.join_policy` |
| B5 op-level cap | only per-persona cap | Phase 2: `policy.max_rounds` |
| B7 unilateral close | anyone with cap can close | Phase 2: `policy.close_policy` + RATIFY |
| B10 context unbounded | unlimited prompt growth | Phase 2: `policy.context_compaction` |
| B20 bot opener | bots can't open | `policy.bot_open=true` (default) |

C / D / E clusters (identity, reliability, deployment) are out of scope
for the protocol layer — separate work.

## Migration path for existing deployments

Phase 1 is **drop-in compatible**:
- Old clients keep working; new fields are ignored if absent.
- Old ops auto-acquire the default policy on first inspection.
- DB schema unchanged (everything in existing JSON columns).

Phase 2 is **opt-in per op**:
- Each op declares its policy at open. Ops opened without policy keep
  `opener_unilateral` close behavior — exactly v2 semantics.
- Enforcement only kicks in for ops that explicitly opted into the
  stricter policy.

Phase 3 retires the shims after a deprecation window.

## Open questions for phase 2

1. **`MOVE_CLOSE` and `RATIFY`: new speech kinds or new event kinds?**
   The contract has `SPEECH_KINDS` already; adding `move_close` /
   `ratify` requires updating `conversation_schemas.SpeechKind` (the
   pydantic Literal). Alternative: a new event kind `chat.governance.*`
   namespace that doesn't pollute speech vocabulary.

2. **Where does the `@operator` role live?** Today actor roles are
   `opener`/`addressed`/`speaker`/etc. on `OperationParticipantV2Model`.
   `policy.close_policy=operator_ratifies` needs a way to identify which
   participant has the operator role. Probably: extend the role enum
   with `operator` and let opener invite by role.

3. **Auto-summary cost.** `rolling_summary` requires running an LLM.
   Either the bridge runs it (needs an API key — contradicts the "no
   API key on bridge" line) or a designated agent runs it on demand
   (cleaner but needs a kernel-level task primitive).

4. **`by_round_seq` enforcement loop.** Where does it live? Current
   `recovery_loop` could pick it up but its cadence is coarse. A
   dedicated lightweight scheduler may be cleaner.
