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
zero regressions vs the prior 471/472 baseline. (The full Phase 1+2+2.5
suite is **512/513 pass** — see status block at the bottom.)

## Phase 2 — opt-in enforcement (DONE for 3/6, deferred for 3/6)

### Implemented in this round

| Component | Enforcement |
|---|---|
| `kernel/v2/policy_engine.py` | New module. `PolicyEngine.check_speech_admissible` + `check_close_admissible`. Stable error codes (`policy.max_rounds_exhausted`, `policy.reply_kind_rejected`, `policy.close_needs_operator_ratify`, `policy.close_needs_quorum`, `policy.close_needs_participant`) for client-side mapping. |
| `kernel/v2/repository.py` | `count_events()` for fast cap checks without paginating the log. |
| `kernel/v2/contract.py` | Added `move_close` + `ratify` to `SPEECH_KINDS`. Drift detector keeps schema and contract aligned. |
| `behaviors/chat/conversation_schemas.py` | `SpeechKind` Literal extended for `move_close` / `ratify`. |
| `behaviors/chat/conversation_service.py` | `submit_speech` runs `check_speech_admissible` before mirror writes. `close_conversation` runs `check_close_admissible` after the legacy capability gate. `bypass_task_guard` paths skip enforcement (system / lease-driven closes keep their authority). |
| `pc_launcher/.../agent_loop.py` | `_ALLOWED_SPEECH_KINDS` now includes `move_close` + `ratify` so personas can use those prefixes. |
| `tests/test_kernel_v3_policy_engine.py` | 9 new tests: max_rounds (cap + unset noop), kind whitelist (reject + wildcard), close policies (default unilateral, any_participant, operator_ratifies — block until role + ratify, quorum — N distinct + de-dup, invalid `min_ratifiers=0` rejected). |

### Enforcement turns on automatically

Default policy (`close_policy=opener_unilateral`, no `max_rounds`)
keeps exact v2 semantics — engine is a no-op. Stricter policies
declared at op-open time activate the corresponding gates.

### Wire contract for clients

`ChatConversationStateError` carrying `policy: <detail>` is the
current-shape signal that an enforcement gate fired. Phase 2.5 will
promote these to a typed HTTP 409 response with the engine's error
code so clients can branch on machine-readable codes:

```
POST /v2/operations/{id}/close → 409 Conflict
{ "code": "policy.close_needs_operator_ratify",
  "detail": "close_policy=operator_ratifies requires a chat.speech.ratify
             event from a participant with role='operator'" }
```

## Phase 2.5 — multi-turn safety net (DONE for 4/5)

After the persona live exercise revealed that v3 worked on single-round
scenarios but had untested holes for longer ops, this round closed the
gaps that prevent multi-turn collaboration from drifting silently.

### Implemented

| Component | Closes |
|---|---|
| `kernel/v2/policy_sweeper.py` | **`by_round_seq` auto-DEFER**. Background loop (default 30s) scans open ops, finds events whose `expected_response.by_round_seq` has elapsed, emits `speech.defer` on the addressee's behalf. Idempotent (won't double-fire on the same trigger). Wired into bridge lifespan. |
| `kernel/v2/policy_engine.py` | **`defer` is universally admissible.** A targeted `kinds=[answer]` whitelist no longer blocks the sweeper from doing its job — defer is the canonical "I cannot answer in the requested form" signal. |
| Two new speech kinds: `move_close`, `ratify` | Phase 2 governance acts. Drift detector aligned across `SPEECH_KINDS` ↔ pydantic Literal ↔ `agent_loop._ALLOWED_SPEECH_KINDS`. |
| Two new speech kinds: `invite`, `join` | **Mid-collab membership protocol**. `speech.invite` from existing participant addresses an outside handle (auto-adds them as `role=addressed`). `speech.join` is self-declaration, gated by `policy.join_policy`. PolicyEngine's `check_invite_admissible` blocks bootstrap-self-invites; `check_join_admissible` enforces `invite_only` ⇒ existing-participant requirement. |
| `db.py` | **`PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.** Fixes the SQLite contention HTTP 500 we saw mid-T5 in the persona run, AND closes a long-tolerated dangling-FK quirk in `replies_to_speech_id`. |
| `agent_loop.py` | **Closed-op skip.** Before each handle, agent probes `GET /v2/operations/{id}` and skips when `state=closed`. Prevents wasted claude runs on stale events from already-closed ops. |
| `api/v2_inbox.py` | **`GET /v2/operations/discoverable?for=@actor`.** Lists open ops the asker is **not yet a participant of** but **could legitimately join** under each op's `join_policy`. Closes the discovery gap surfaced in the mid-collab join review. |
| `tests/test_kernel_v3_adversarial.py` | 7 negative-path tests pinning down that gates *reject* misbehavior, not just allow conformant clients. |
| `tests/test_kernel_v3_multiturn_convergence.py` | 2 tests of 10-round disagreement→propose→ratify→close convergence. |
| `tests/test_kernel_v3_policy_sweeper.py` | 4 sweeper behavior tests (emit / idempotent / skip-on-reply / pre-window noop). |
| `tests/test_kernel_v3_join_invite.py` | 5 membership tests covering all three join policies. |
| `tests/test_kernel_v3_late_join_privacy.py` | 2 tests confirming a late-joiner's history fetch redacts whisper events from before they joined. |
| `tests/test_kernel_v3_discovery.py` | 5 endpoint tests (open/self_or_invite/invite_only filtering, exclude already-in / closed, space_id filter). |

### Deferred to a later round

1. **`context_compaction=rolling_summary`.** Generating summaries
   needs an LLM caller. The kernel deliberately doesn't carry an
   API key (pivot earlier this work). Right shape: a designated
   external agent (e.g. `@summarizer`) listens for compaction
   triggers and posts `speech.summarize` artifacts. Out of scope
   for protocol primitives — it's an *agent application* on top.

2. **SSE catch-up replay.** A new joiner's SSE only delivers events
   posted after subscribe. Catch-up is via REST `GET /events`. The
   `agent_loop._fetch_op_history` already covers this for agent
   clients; UI clients would benefit from a `?from_seq=N` SSE replay
   mode but that's a UX nicety, not a correctness gap.

3. **Agent presence / heartbeat.** When an agent crashes mid-run,
   the trigger event sits unanswered until `by_round_seq` elapses
   and the sweeper auto-defers. Faster signal would require an
   agent-level heartbeat (`actor.last_seen_at` already exists in
   the schema; just nothing updates it from the agent side).

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
   dedicated lightweight scheduler may be cleaner. **(Resolved in
   phase 2.5: `kernel/v2/policy_sweeper.py` is the dedicated loop,
   default 30s cadence, opt-out via `BRIDGE_POLICY_SWEEPER_SECONDS=0`.)**

---

## Status block (final, this work cycle)

| Layer | State |
|---|---|
| Phase 1 (additive primitives) | ✅ Done |
| Phase 2 (policy enforcement) | ✅ Done — max_rounds, kind whitelist, close policy |
| Phase 2.5 (multi-turn safety net) | ✅ Done — sweeper, JOIN/INVITE, closed-op skip, late-join privacy, discovery, WAL |
| Phase 3 (legacy cleanup) | ⏸ Pending — BROADCAST env, max_per_op env, addressed_to_many semantics |
| Out of scope | rolling_summary (needs `@summarizer` agent), agent presence/heartbeat, identity hardening |

### Test counts

| Suite | Tests | Status |
|---|---|---|
| Pre-v3 baseline | 471 | passed |
| + v3 phase 1 (`test_kernel_v3_speech_act_primitives.py`) | +7 | passed |
| + v3 phase 2 (`test_kernel_v3_policy_engine.py`) | +9 | passed |
| + v3 phase 2.5 adversarial (`test_kernel_v3_adversarial.py`) | +7 | passed |
| + v3 phase 2.5 multi-turn (`test_kernel_v3_multiturn_convergence.py`) | +2 | passed |
| + v3 phase 2.5 sweeper (`test_kernel_v3_policy_sweeper.py`) | +4 | passed |
| + v3 phase 2.5 join/invite (`test_kernel_v3_join_invite.py`) | +5 | passed |
| + v3 phase 2.5 late-join privacy (`test_kernel_v3_late_join_privacy.py`) | +2 | passed |
| + v3 phase 2.5 discovery (`test_kernel_v3_discovery.py`) | +5 | passed |
| **Aggregate** | **512** | **passed** (1 skipped = live LLM opt-in) |

### Live verification snapshots (5/5 PASS)

The post-Phase-2 persona run is captured in
[protocol-personas-live-log.md](./protocol-personas-live-log.md). The 5
tasks are: targeted Q&A (cascade prevention), broadcast collab,
speech-kind whitelist, any_participant close, max_rounds cap. All
passed end-to-end via real claude CLI personas through external
`agent_loop`.

### What's honest to claim

- **Cooperative single-bridge multi-agent collab is structurally sound.**
  Cascade prevention, kind enforcement, close policy, max_rounds,
  membership, late-join privacy, discovery — all backed by the test
  suite + a live persona run.
- **Adversarial robustness at the message-semantics layer is real.**
  7 negative-path tests confirm the gates reject misbehavior, not
  just allow good behavior.
- **Identity / authentication remains the gaping hole.** Single
  shared bearer token; any caller can claim any handle. The
  capability model exists in code but is functionally a no-op
  because there's no per-actor principal. Production exposure
  outside a trusted LAN requires Phase 3.x identity work first.
