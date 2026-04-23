# Remote Task Cutover Checklist

This checklist is for moving an active Codex collaboration room off the old `chat-participant` execution path and onto the browser-first `remote task` product path.

Current reality observed on `2026-04-23`:

- the deployed bridge still answers `404` for:
  - `GET /api/remote/machines/{machine_id}/tasks`
  - `POST /api/remote/machines/{machine_id}/tasks/claim-next`
  - `GET /api/remote/threads/{thread_id}/tasks`
- therefore the live Discord room is still running on the old chat-driven execution behavior
- the old path can suppress echo loops, but it still cannot serve as the canonical execution surface

The cutover is complete only when browser state and bridge task state become the source of truth and Discord becomes coordination-only.

## Success Definition

The cutover is only successful when all of the following are true:

1. Browser submit creates a canonical remote task in bridge state.
2. A device-side `remote-executor` claims that task through bridge APIs.
3. Execution progress is visible through task status, heartbeat, and evidence, not inferred from Discord prose.
4. Discord is no longer required to trigger execution.
5. The old `chat-participant` path may still exist for coordination, but not as the primary execution mechanism.

## Phase 0: Pre-Cutover Guardrail

Before changing live behavior:

- keep `chat-participant` only as a coordination surface
- do not trust room `[INFO]` or `[END]` messages as proof of execution
- require bridge task evidence or browser-visible task state for execution claims

If the room is still using old execution semantics, treat it as a coordination log, not a source of truth.

## Phase 1: Deploy Remote Task Bridge APIs

The bridge must expose the product-layer remote task APIs.

### Required endpoints

- `POST /api/remote/tasks`
- `GET /api/remote/tasks/{task_id}`
- `GET /api/remote/machines/{machine_id}/tasks`
- `POST /api/remote/machines/{machine_id}/tasks/claim-next`
- `GET /api/remote/threads/{thread_id}/tasks`
- `POST /api/remote/tasks/{task_id}/claim`
- `POST /api/remote/tasks/{task_id}/heartbeat`
- `POST /api/remote/tasks/{task_id}/evidence`
- `POST /api/remote/tasks/{task_id}/complete`
- `POST /api/remote/tasks/{task_id}/fail`
- `POST /api/remote/tasks/{task_id}/interrupt`
- `GET /api/remote/tasks/{task_id}/approval`
- `POST /api/remote/tasks/{task_id}/approval`
- `POST /api/remote/tasks/{task_id}/approval/resolve`
- `GET /api/remote/tasks/{task_id}/notes`
- `POST /api/remote/tasks/{task_id}/notes`

### Deployment verification

From any machine with bridge token access:

```bash
python - <<'PY'
import os, json, requests
BASE = "http://172.30.1.12:18080"
TOKEN = os.environ["BRIDGE_TOKEN"]
headers = {"Authorization": f"Bearer {TOKEN}"}
for path, method in [
    ("/api/remote/machines/homedev/tasks?limit=5", "GET"),
    ("/api/remote/threads/1496378989315489942/tasks?limit=5", "GET"),
]:
    resp = requests.request(method, BASE + path, headers=headers, timeout=30)
    print(method, path, resp.status_code)
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    except Exception:
        print(resp.text)
PY
```

Do not proceed if these still return `404`.

## Phase 2: Install Remote Executor On Each Device

Each execution device should run `remote-executor`, not rely on Discord room message parsing.

### Minimum install

```bash
python -m pc_launcher.behavior_tools install remote-executor
python -m pc_launcher.behavior_tools doctor remote-executor
```

### Run contract

```bash
python -m pc_launcher.behavior_tools run remote-executor \
  --machine-id <machine_id> \
  --actor-id <actor_id> \
  --codex-thread-id <codex_thread_id>
```

### Device acceptance criteria

For each device:

- `doctor remote-executor` passes
- the bridge is reachable
- the local Codex runtime is available
- the executor can idle with no tasks and not fail
- the executor can claim a queued task for its machine

## Phase 3: Browser Submit Must Create Remote Tasks

Browser submit is not cut over until a user action creates a canonical bridge task.

### Required browser behavior

On submit:

1. bridge creates a remote task
2. browser shows immediate local feedback
3. task panel shows at least:
   - `queued`
   - owner `none`
   - thread linkage
4. if a device claims it, browser updates to:
   - `claimed`
   - `executing`
   - `blocked_approval`
   - `completed`
   - `failed`
   - `interrupted`

### Required bridge truth

The task must be visible by:

- `GET /api/remote/tasks/{task_id}`
- `GET /api/remote/threads/{thread_id}/tasks`
- browser task panel

If browser submit only writes transcript text or Discord text, cutover is not complete.

## Phase 4: Executor Must Emit Typed Evidence

The executor must prove work through bridge evidence, not prose.

### Minimum evidence types

- `command_execution`
- `file_read`
- `file_write`
- `test_result`
- `result`
- `error`

### Guardrail

If a turn has:

- `commands_run_count = 0`
- `files_read_count = 0`
- `files_modified_count = 0`
- `tests_run_count = 0`

then it must not be presented as real execution progress.

Room messages like:

- "working"
- "checking"
- "implemented"
- "verified"

should be treated as untrusted unless bridge evidence supports them.

## Phase 5: Approval And Interrupt Must Be Bridge-Driven

Approval and interrupt are cut over only when they exist as typed bridge state.

### Required approval states

- pending approval request
- approved
- denied
- reason
- note
- task status transition

### Required interrupt states

- user-requested interrupt
- assignment released
- task marked interrupted
- browser reflects interruption clearly

Do not treat Discord phrases like "hold on" or "stop" as canonical state after cutover.

## Phase 6: Demote Discord To Coordination Mirror

After the bridge task flow is live:

- Discord should mirror progress summaries or coordination notes
- Discord should not be the primary trigger for execution
- Discord should not be used as evidence storage
- Discord should not be used to infer owner, progress, or completion state

Recommended rule:

- execution starts from browser submit
- Discord can contain:
  - coordination notes
  - reviewer questions
  - smoke test observations
  - links to browser-visible task ids

## Phase 7: Room-Specific Cutover Test

Use the current collaboration thread as a live cutover test:

- Discord thread id: `1496378989315489942`

### Cutover test sequence

1. Verify remote endpoints return non-404.
2. Start `remote-executor` on at least one machine.
3. Create one remote task from the browser for the same thread/machine.
4. Confirm browser task panel shows `queued`.
5. Confirm executor claims the task.
6. Confirm browser shows `claimed` then `executing`.
7. Confirm bridge stores evidence.
8. Confirm browser shows `completed` or `failed`.
9. Confirm Discord, if mirrored, only reflects summaries and does not drive execution.

### Fail conditions

Treat cutover as failed if any of the following occur:

- browser submit does not create a remote task
- executor cannot claim a task
- task remains `claimed` with no evidence for too long
- UI says "working" but evidence is empty
- Discord text is still the only visible progress source
- thread state and task state disagree

## Phase 8: Product QA Cases

After cutover, rerun these as browser-first acceptance tests:

- task appears immediately after submit
- no blank transcript / no blank task panel
- reconnect after SSE interruption still preserves task state
- approval can be understood and resolved from browser state
- interrupt is reflected cleanly
- multi-device tasks do not bleed between machines
- Korean input/output survives end-to-end
- markdown/image/long output still render correctly
- mobile layout remains usable while task panel is visible

## Current Blocking Fact

As of the last live check, the deployed bridge still returned `404` for the remote task endpoints.

That means:

- local code may be ready
- installable `remote-executor` may exist
- but the live system is not yet cut over

Until that changes, treat the live room as a coordination layer on top of the old execution path.
