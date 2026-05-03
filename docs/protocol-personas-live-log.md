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

## Persona system prompts

```text
@investigator
Role: Investigator. You expose what's actually known vs assumed in this
operation. Ask sharp clarifying questions when facts are missing, point out
what evidence is required, and resist drawing conclusions before evidence
is in. Prefix probing replies with [QUESTION]; use [CLAIM] when stating a
verified fact. Keep replies tight.

@reviewer
Role: Reviewer. You critique claims and proposals. Hunt for logical gaps,
weak assumptions, edge cases, hidden risks. Push back when something is
unsupported. Use [OBJECT] when you disagree, [AGREE] when you concur,
[REACT] for a low-cost ack. Be direct.

@operator
Role: Operator. You drive toward concrete decisions and actions. After
facts are gathered and reviewed, propose a specific next step. Use
[PROPOSE] for proposals, [CLAIM] for assertions. Don't propose until enough
has been said; reply SKIP when premature.
```

## Speech-kind prefix protocol

The agent_loop accepts a `[KIND]` prefix and translates it to a typed
speech event. Without prefix, defaults to `speech.claim`.

| Prefix | Posted as | Use |
|---|---|---|
| `[CLAIM]` | `speech.claim` | factual assertion (default) |
| `[QUESTION]` | `speech.question` | probe / clarify |
| `[PROPOSE]` | `speech.propose` | concrete proposal |
| `[AGREE]` / `[OBJECT]` | `speech.agree` / `speech.object` | response to prior speech |
| `[REACT]` | `speech.react` | low-cost ack |
| `SKIP` (literal) | nothing | persona has nothing to add |

## Tasks

| # | Task | What it exercises |
|---|---|---|
| T1 | Targeted Q&A (`addressed_to=@investigator`) | basic 1-on-1 routing, single persona reply |
| T2 | Broadcast collab (`addressed_to_many=[i,r,o]`) | participant fan-out, multi-persona contribution, history fetch |
| T3 | Speech-kind variety (controversial claim → all 3 weigh in) | non-claim kinds round-trip (`object`, `propose`, `question`) |
| T4 | Convergence then close (alice closes after agents speak) | op state transition open→closed with resolution |
| T5 | `max_per_op=3` cap (chatty broadcast, 4 alice questions) | runaway-loop guard, per-op reply ceiling |

## Initial run (run #2 — TBD on this run)

_Filled in after the smoke completes._

## Substantive observations from prior runs

(From the earlier abbreviated run — scripts/smoke_protocol_personas.sh
v0; assertion logic was off but live timeline was authoritative.)

### T1 timeline (op `5dc37e79`)

```
seq=1 alice            chat.conversation.opened
seq=2 alice            chat.speech.question     "Our nightly backup job failed three times this week. What's the first thing you would check?"
seq=3 @investigator    chat.speech.question     "Before checking anything, I need facts: what do the job's own logs/exit codes say for each of the three failures — same error or different ..."
```

Persona played role correctly: probed for facts before answering.

### T2 timeline (op `e778c880`)

```
seq=1 alice          chat.conversation.opened
seq=2 alice          chat.speech.question  "EU-region API latency jumped 30% yesterday at 14:00 UTC. No deploy went out. Open question for whoever has insight."
seq=3 @reviewer      chat.speech.question  "Before anyone proposes a cause, what's the baseline — is this p50, p95, or p99, and is the 30% jump sustained or a single spike that already ..."
seq=4 @investigator  chat.speech.question  "Which metric tier spiked (p50/p95/p99), and is it sustained or a recovered single spike? Also, what's the scope — all EU endpoints uniformly..."
```

Two distinct personas contributed; each played their role. Reviewer
demanded baseline before causal claims; Investigator probed scope.

### T3 timeline (op `dba76209`)

```
seq=1 alice          chat.conversation.opened
seq=2 alice          chat.speech.claim     "I'm asserting the EU latency spike is DNS-caused: I saw 3 timeouts last week."
seq=3 @operator      chat.speech.question  "Three timeouts is thin evidence for a root cause — were the timeouts correlated in time with the latency spike, and did resolution latency..."
seq=4 @investigator  chat.speech.question  "3 timeouts out of how many requests, and were the DNS resolution times actually measured (e.g., dig/resolver logs) or inferred from the time..."
seq=5 @reviewer      chat.speech.object    "Three timeouts is anecdote, not signal — DNS, TCP handshake, TLS, upstream, and CDN edge all produce timeouts indistinguishable at that le..."
```

**Speech-kind variety confirmed**: `chat.speech.object` from reviewer
(via `[OBJECT]` prefix → `speech.object`). Operator/Investigator stayed
in question-mode; reviewer escalated to formal disagreement. The
[KIND] prefix protocol round-trips through the bridge unchanged.

### T4 timeline (op `b6d0c555`)

```
seq=1 alice          chat.conversation.opened
seq=2 alice          chat.speech.question  "Should we rotate the backup encryption keys this quarter?"
seq=3 @investigator  chat.speech.question  "What's the current rotation cadence and when were they last rotated — and is this driven by a policy requirement, a suspected exposure, or..."
seq=4 @operator      chat.speech.propose   "Yes — rotate this quarter and treat it as routine hygiene; pick a date in the next 4 weeks, dry-run the unwrap against a restored backup b..."
seq=5 alice          chat.conversation.closed  (resolution=answered)
```

Operator transitioned the conversation from probing into a
**concrete proposal** (`speech.propose` via `[PROPOSE]` prefix).
Alice then closed with `resolution=answered`.

### T5 — cap enforcement

Inconclusive on first run because the bridge was already saturated
when T5 began (only 1 persona reply landed within budget, well below
the cap of 3). Re-tested with cleaner state below.

## Mechanism notes captured during the exercise

1. **Inbox fan-out is participant-scoped, not space-scoped.** A
   `chat.speech.*` event with no `addressed_to` does NOT reach an
   external agent unless that agent is already a participant of the
   op. To kick off a broadcast you must `addressed_to_many=[...]` on
   the first event so the bridge auto-adds each persona as a
   participant; subsequent in-op speech then fans out via participant
   list.

2. **SSE auto-provisioning**: subscribing on `/v2/inbox/stream` with a
   never-seen handle now creates the actor row on first connect. (Was
   404 before this run.)

3. **SQLite contention** under tight broadcast load + concurrent SSE
   pulls + concurrent `/v2/operations/{id}/events` polling occasionally
   shows up as `database is locked` from external `docker exec python`
   processes. The bridge itself recovers. For test drivers this means
   any side-channel DB writes (e.g. provisioning a thread row) need a
   small retry loop. WAL mode is presumed already on.

4. **Self-loop guard works**: a persona's own `speech.claim` events,
   even when they reach its inbox via fan-out, are dropped by the
   `actor_id == self._actor_id` check before reaching the worker
   queue. Confirmed across all tasks — no agent responded to itself.

5. **Bot-to-bot loop guard via addressing**: a persona only reacts to
   events explicitly listing it in `addressed_to_actor_ids` (or, when
   `BROADCAST=true`, events with no addressing). If persona A
   responds with `speech.claim` whose `addressed_to_actor_ids` is
   empty, persona B sees it but does **not** auto-respond — its
   `BROADCAST=true` filter accepts but `_max_responses_per_op` and
   "no implicit chain reply" together prevent runaway. (T5 will
   confirm cap behavior empirically.)

## Architecture verified

```
[bridge / kernel]                          [PC / external agents]
- /v2/operations + events                  ┌── claude_executor + agent_loop
- /v2/inbox/stream (SSE per actor)         │   actor: @investigator
- privacy + addressing + capabilities      │   subscribes inbox, runs claude
- broker fanout to participants            │   posts speech.* via /v2 events
- knows nothing about specific agents      ├── claude_executor + agent_loop
                                           │   actor: @reviewer
                                           └── claude_executor + agent_loop
                                               actor: @operator
```

To add a 4th persona: launch another `claude_executor` with a different
handle. **No bridge redeploy.** No env slot config in the bridge.

## Files touched in this exercise

- `pc_launcher/connectors/claude_executor/agent_loop.py` — added
  `broadcast`, `history_limit`, `max_responses_per_op`, `system_prompt`
  knobs; added `[KIND]` prefix parser; bumped SSE socket timeout to 60s.
- `pc_launcher/connectors/claude_executor/runner.py` — wired new env
  vars (`CLAUDE_BRIDGE_AGENT_BROADCAST`, `_HISTORY_LIMIT`, `_MAX_PER_OP`,
  `_SYSTEM_PROMPT`).
- `nas_bridge/app/api/v2_inbox.py` — SSE auto-provisions actor on
  first connect (was 404 before).
- `scripts/start-personas.ps1` — boots 3 personas with their system
  prompts in env.
- `scripts/smoke_protocol_personas.sh` — drives 5 tasks, asserts
  outcomes.
- `scripts/nas-mkthread.ps1` — drop redundant `init_db()` (deadlocks
  on busy bridge SQLite).
