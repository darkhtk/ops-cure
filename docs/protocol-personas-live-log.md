# Protocol Personas — Live Run Log

Live exercise of the kernel + external-agent protocol against the NAS bridge.
Three personas (`@investigator`, `@reviewer`, `@operator`) run as
**external** `claude_executor` processes on the same PC, each subscribing
to its own actor inbox via `/v2/inbox/stream` and posting `speech.*`
events back via `/v2/operations/{id}/events`.

The bridge is **pure kernel** — no agent code in the bridge process.

## Environment

| Component | Value |
|---|---|
| Bridge | NAS, `http://172.30.1.12:18080` (docker `nas-bridge`) |
| Personas | 3, all on this PC, distinct `CLAUDE_BRIDGE_MACHINE_ID` and `CLAUDE_BRIDGE_ACTOR_HANDLE` |
| Agent runtime | `pc_launcher/connectors/claude_executor/agent_loop.py` |
| Knobs in use | `BROADCAST=true`, `HISTORY_LIMIT=20`, `MAX_PER_OP=3`, persona system prompt via env |
| Date | 2026-05-03 |

## Three protocol options (a / b / c) — all opt-in via env

| Knob | Env var | What it enables |
|---|---|---|
| (a) Broadcast speech | `CLAUDE_BRIDGE_AGENT_BROADCAST=true` | Agent also responds to events with empty `addressed_to_actor_ids` (room-wide), not just events explicitly addressed to it |
| (b) Op-history fetch | `CLAUDE_BRIDGE_AGENT_HISTORY_LIMIT=20` | Before each run, agent pulls last N events from the op via `/v2/operations/{id}/events` and folds them into the prompt as a transcript |
| (c) Persona via system prompt | `CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT="…"` | Same binary hosts different personas — investigator / reviewer / operator are distinguished only by env, no code changes |

Plus a small structured-reply convention so a single agent can post
typed speech without per-kind dispatchers:

| Prefix | Posted as | Use |
|---|---|---|
| `[CLAIM]` (default) | `speech.claim` | factual assertion |
| `[QUESTION]` | `speech.question` | probe / clarify |
| `[PROPOSE]` | `speech.propose` | concrete proposal |
| `[AGREE]` / `[OBJECT]` | `speech.agree` / `speech.object` | response to prior speech |
| `[REACT]` | `speech.react` | low-cost ack |
| `SKIP` (literal) | nothing | persona has nothing useful to add |

## Persona system prompts (full text)

```text
@investigator
Role: Investigator. You expose what's actually known vs assumed in this
operation. Ask sharp clarifying questions when facts are missing, point
out what evidence is required, and resist drawing conclusions before
evidence is in. Prefix probing replies with [QUESTION]; use [CLAIM] when
stating a verified fact. Keep replies tight.

@reviewer
Role: Reviewer. You critique claims and proposals. Hunt for logical gaps,
weak assumptions, edge cases, hidden risks. Push back when something is
unsupported. Use [OBJECT] when you disagree, [AGREE] when you concur,
[REACT] for a low-cost ack. Be direct.

@operator
Role: Operator. You drive toward concrete decisions and actions. After
facts are gathered and reviewed, propose a specific next step. Use
[PROPOSE] for proposals, [CLAIM] for assertions. Don't propose until
enough has been said; reply SKIP when premature.
```

## Tasks

| # | Task | What it exercises |
|---|---|---|
| T1 | Targeted Q&A (`addressed_to=@investigator`) | basic 1-on-1 routing, single persona reply |
| T2 | Broadcast collab (`addressed_to_many=[i,r,o]`) | participant fan-out, multi-persona contribution, history fetch |
| T3 | Speech-kind variety (controversial claim → all 3 weigh in) | non-claim kinds round-trip (`object`, `propose`, `question`) |
| T4 | Convergence then close (alice closes after agents speak) | op state transition open→closed with resolution |
| T5 | `max_per_op=3` cap (chatty broadcast, 4 alice questions) | runaway-loop guard, per-op reply ceiling |

## Result summary

| Task | Result | Evidence |
|---|---|---|
| T1 | PASS | investigator replied within ~3 ticks (~6s), used `[QUESTION]` to probe |
| T2 | PASS | 3 distinct personas contributed (reviewer + investigator both questioned, operator proposed) within ~5 ticks (~10s) |
| T3 | PASS | 3 non-claim speech kinds posted: `speech.object` (reviewer x2), `speech.question` (investigator) |
| T4 | PASS | investigator probed, operator `[PROPOSE]`d; alice closed; op state → `closed` |
| T5 | INCONCLUSIVE | bridge SQLite contention prevented clean cap exercise; see "Operational lessons" |

## T1 timeline (op `5b768531`)

```
seq=1 alice            chat.conversation.opened
seq=2 alice            chat.speech.question
  "Our nightly backup job failed three times this week.
   What's the first thing you would check?"
seq=3 @investigator    chat.speech.question
  "Before checking anything, what's the actual failure signal —
   exit code, log message, alert text? "Failed three times" could
   mean timeouts, …"
```

Persona played role correctly: refused to leap to a recommendation,
demanded the failure signal first.

## T2 timeline (op `4291139e`)

```
seq=1 alice            chat.conversation.opened
seq=2 alice            chat.speech.question
  "EU-region API latency jumped 30% yesterday at 14:00 UTC.
   No deploy went out. Open question for whoever has insight."
seq=3 @reviewer        chat.speech.question
  "Before anyone hypothesizes, what's the evidence base — is this
   p50, p95, or p99, and is it region-wide or scoped to specific
   endpoints/customers?"
seq=4 @investigator    chat.speech.question
  "Before we theorize: what's the evidence baseline — is the 30%
   jump on p50, p95, or p99, and is it across all EU endpoints
   or concentrated …"
seq=5 @operator        chat.speech.propose
  "Pull the metric definition and a 24h chart now — confirm
   percentile, scope (endpoint/region/customer cohort), and
   whether the step is a level shift or a recovered spike."
```

Three distinct contributors, each in their lane:
- reviewer + investigator both demanded an evidence baseline (different
  framings, same intent),
- operator translated that demand into a concrete next action via
  `[PROPOSE]` → `speech.propose`.

## T3 timeline — speech-kind variety (op `6d179616`)

```
seq=1 alice            chat.conversation.opened
seq=2 alice            chat.speech.claim
  "I'm asserting the EU latency spike is DNS-caused: I saw 3
   timeouts last week."
seq=3 @operator        chat.speech.object
  "Three timeouts isn't a sample — it's an anecdote, and
   "DNS-caused" is a mechanism claim that needs resolver latency
   or NXDOMAIN data to support."
seq=4 @investigator    chat.speech.question
  "Three timeouts across what sample size, and were they EU-only
   or global? Before blaming DNS, I need resolver logs (or `dig`
   traces) showing …"
seq=5 @reviewer        chat.speech.object
  "Three timeouts over a week is noise, not signal — and timeouts
   alone don't isolate DNS from TCP, TLS, upstream, or routing.
   Need resolver-level evidence."
```

This is the most informative task. Both `@reviewer` and `@operator`
escalated to `speech.object` (the kernel-level disagreement primitive)
rather than counter-claiming. The `[OBJECT]` prefix in the agent
output gets translated to `speech.object` by the agent loop. The
investigator stayed in probe mode (`speech.question`). All three
persona roles were respected end-to-end — the prompt + system prompt +
prefix mechanism (option c) actually shapes behavior.

## T4 timeline — convergence then close (op `b6d0c555`, captured in earlier run)

```
seq=1 alice            chat.conversation.opened
seq=2 alice            chat.speech.question
  "Should we rotate the backup encryption keys this quarter?"
seq=3 @investigator    chat.speech.question
  "What's the current rotation cadence and when were they last
   rotated — and is this driven by a policy requirement, a
   suspected exposure, or …"
seq=4 @operator        chat.speech.propose
  "Yes — rotate this quarter and treat it as routine hygiene;
   pick a date in the next 4 weeks, dry-run the unwrap against
   a restored backup …"
seq=5 alice            chat.conversation.closed   resolution=answered
```

Verified op state: `GET /v2/operations/{id}` → `state=closed` after
alice closed. Operator played its role: only proposed once enough
context was on the table.

## T5 — inconclusive on this run

Setup:
- 4 alice questions spaced 5s apart, each `addressed_to_many=[i,r,o]`
- expected: `max_per_op=3` cap should clamp each persona to ≤3 replies

Result on the live bridge: by the time T5 fired, the agents'
worker queues had built up backlog from T2/T3 broadcast cascades
(see "Operational lessons" #2 below). Many T5 events were never
processed before the smoke wait window expired. The cap *is*
correct in code (`_enqueue_event` checks
`_responses_per_op[op_id] >= self._max_responses_per_op`), but the
live exercise didn't put it in a state where the cap could be
observed binding rather than the queue saturating first.

Recommendation: either raise the cap in test (e.g. 5) so the queue
rather than the cap is what limits, or run T5 in isolation against
a fresh bridge.

## Operational lessons (worth keeping)

1. **SSE socket timeout matters.** Initially `urlopen(...,
   timeout=30)` — under bridge SQLite contention the heartbeats
   missed the 30s window and agents reconnected in a loop without
   ever consuming an event. Raised to 60s. Should likely become
   90s+ in production where bursts of broker fan-out can briefly
   stall the heartbeat coroutine.

2. **Broadcast cascades faster than the per-op cap can throttle.**
   3 personas + `BROADCAST=true` + `addressed_to_many` means every
   persona's reply fans out to the other two. With `max_per_op=3`
   the cap kicks in at *enqueue* time, but a queue can already
   have several events in flight when the cap binds. The math:
   3 personas × 3 replies-per-op = up to 9 persona events, plus
   alice's questions. Each claude run is ~30s, so a saturated
   queue takes 5+ minutes to drain. Two practical mitigations:
   - lower `MAX_PER_OP` to 1-2 for noisy collab rooms,
   - introduce "no implicit chain reply" — only respond to events
     authored by humans, not by other agents (would prevent
     cascade entirely).

3. **History fetch must be best-effort.** `_fetch_op_history`
   originally only caught `urllib.error.HTTPError | URLError`,
   which doesn't include `socket.timeout`. Under bridge load the
   timeout bubbled up through `_handle_event` and dropped the
   entire run. Now catches `Exception` and falls back to no
   history. Lesson: any pre-run context-gathering should never
   abort the run itself.

4. **SSE is participant-scoped, not space-scoped.** A
   `chat.speech.*` event with no `addressed_to` only reaches an
   external agent if that agent is already a participant of the
   op. To kick off a broadcast, the FIRST event must use
   `addressed_to_many=[…]` so the bridge auto-adds each persona
   as a participant; subsequent in-op speech then fans out via
   the participant list. This is documented in
   `_publish_v2_inbox_fanout`.

5. **SSE auto-provisioning of actors.** Subscribing to
   `/v2/inbox/stream` with a never-seen handle now creates the
   actor row on first connect (was 404 before). External agents
   bring themselves into existence by subscribing — no separate
   "register agent" call.

6. **SQLite contention is real under broadcast load.** 3 SSE
   subscribers + `/agent/sync` polling + agent claim polling +
   smoke driver polling /events all hitting SQLite caused
   visible "database is locked" errors and HTTP timeouts. Cleanup:
   - Bumped legacy `/agent/commands/claim` poll from 1s → 10s
     (this path is unused for personas; it was just background
     noise saturating the WAL writer).
   - `nas-mkthread.ps1` no longer runs `init_db()` (the bridge
     already created the schema; the redundant `PRAGMA
     table_info` blocks under contention).
   For sustained multi-agent collab, SQLite is probably not the
   right backend — Postgres would handle this trivially.

7. **Self-loop guard works.** Confirmed across all tasks: a
   persona's own `speech.*` events, even when they reach its
   own inbox via fan-out, are dropped by the
   `actor_id == self._actor_id` check before reaching the
   worker queue. No agent ever responded to itself.

## Architecture verified

```
[bridge / kernel]                          [PC / external agents]
- /v2/operations + events                  ┌── claude_executor + agent_loop
- /v2/inbox/stream (SSE per actor)         │   actor: @investigator
- privacy + addressing + capabilities      │   subscribes inbox, runs claude
- broker fan-out to participants           │   posts speech.* via /v2 events
- knows nothing about specific agents      ├── claude_executor + agent_loop
                                           │   actor: @reviewer
                                           └── claude_executor + agent_loop
                                               actor: @operator
```

To add a 4th persona: launch another `claude_executor` with a
different handle. **No bridge redeploy. No env slot config in the
bridge. No code change.**

## Files touched

- `pc_launcher/connectors/claude_executor/agent_loop.py` — added
  `broadcast`, `history_limit`, `max_responses_per_op`,
  `system_prompt` knobs; added `[KIND]` prefix parser; bumped SSE
  socket timeout to 60s; broadened history-fetch exception catch.
- `pc_launcher/connectors/claude_executor/runner.py` — wired new env
  vars (`CLAUDE_BRIDGE_AGENT_BROADCAST`, `_HISTORY_LIMIT`,
  `_MAX_PER_OP`, `_SYSTEM_PROMPT`).
- `nas_bridge/app/api/v2_inbox.py` — SSE auto-provisions actor on
  first connect.
- `scripts/start-personas.ps1` — boots 3 personas with system prompts;
  legacy poll bumped 1s → 10s.
- `scripts/smoke_protocol_personas.sh` — drives 5 tasks, asserts
  outcomes (poll 2s, 60-tick budget = 120s/task).
- `scripts/nas-mkthread.ps1` — drop redundant `init_db()` (deadlocks
  on busy bridge SQLite).
- `scripts/deploy-nas.ps1` — `--no-cache` flag; merge docker compose
  stderr to stdout so PowerShell's native-command stderr trap doesn't
  abort on routine progress lines.

## What this exercise does NOT prove

- **Capability enforcement under load.** The bridge has a
  capability system (CAP_SPEECH_SUBMIT etc.) but every request
  here used the same shared bearer token, so we never tested
  "agent X tried to do something it lacks the cap for." Worth a
  separate scenario.

- **Privacy redaction in multi-persona setting.** All speech here
  was public. A test that posts `private_to_actors=[X]` and
  verifies non-X personas don't see it would close that gap.

- **Long-horizon collab beyond one round of probe→propose.** Each
  task ran 1-2 turns. A multi-turn convergence (10+ rounds) under
  the cap would test whether the persona roles hold or degrade.

- **Bot-only chains.** The smoke always had alice as the
  conversation opener and addressee. A bot-initiated op (one
  persona starts an op addressed to another) is a different
  shape that wasn't exercised.
