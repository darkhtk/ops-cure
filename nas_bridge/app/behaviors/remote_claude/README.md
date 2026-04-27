# `remote_claude` Behavior

Browser-first remote Claude Code behavior. Mirrors the structure of
`remote_codex` but is wired around claude's `--print --input-format
stream-json` lifecycle (one OS process per session, multiple turns over
stdin).

```text
remote_claude/
  __init__.py
  api.py            FastAPI router under /api/remote-claude
  service.py        BehaviorService -- thin facade, delegates to state_service
  state_service.py  DB ops + pub/sub. Mirrors all events to the kernel broker.
  kernel_binding.py Synthetic kernel spaces + KernelBehaviorBinding registration.
```

The PC-side counterpart lives at
`pc_launcher/connectors/claude_executor/`; it spawns the local `claude`
CLI, streams stream-json events back, and polls / claims commands on
behalf of this behavior.

## Ownership split

- **Opscure** (this package + state_service) owns canonical session /
  command / machine state.
- **claude-remote** (separate repo) owns the browser site that renders
  + drives this behavior.
- **claude_executor** (in pc_launcher) is the device-side adapter that
  spawns the claude CLI process and reports stream-json events.

## Event transport — kernel subscription broker

`state_service._mirror_to_kernel_broker` publishes every behavior-local
event onto the kernel `subscription_broker`. Browser + agent subscribe via
the generic `/api/events/spaces/{space_id}/stream` SSE channel that
`chat`, `ops`, and `remote_codex` also use -- one transport, four
behaviors.

Synthetic spaces:

| Space id | Carries |
|---|---|
| `remote_claude.machine:{machine_id}` | command lifecycle (`remote_claude.command.queued/running/completed/failed`), session list updates (`remote_claude.session`), machine status |
| `remote_claude.session:{session_id}` | per-session stream-json events (`claude.event`, `claude.stderr`, `claude.exit`, `claude.parse_error`, `adapter.meta`) |

Each `EventEnvelope.event.content` is the JSON-serialized legacy payload
(`{"kind": "claude.event", "event": {...}}`, etc.) so the existing
client-side dispatch logic still works -- the frontend just listens for
the generic `event: event` SSE name and dispatches off `payload.kind`.

The legacy behavior-specific SSE endpoints
(`/machines/{m}/live`, `/sessions/{sid}/live`) and the in-memory ring
buffer they relied on were removed. The kernel broker has its own
1024-event per-space backlog + cursor-based replay (resume via
`?after_cursor=...`), so late subscribers don't lose recent events
within that window.

## Endpoints

Browser:

- `GET  /api/remote-claude/machines`                      machine list
- `GET  /api/remote-claude/machines/{m}/sessions`         session list
- `POST /api/remote-claude/machines/{m}/sessions`         start a fresh run
- `POST .../sessions/{sid}/input`                         append a turn
- `POST .../sessions/{sid}/interrupt`                     SIGINT
- `DELETE .../sessions/{sid}`                             unlink jsonl + drop row
- `GET  .../sessions/{sid}/transcript`                    full jsonl history (agent reads disk)
- `GET  /api/remote-claude/machines/{m}/fs/list`          dir listing on the PC
- `POST /api/remote-claude/machines/{m}/fs/mkdir`         mkdir on the PC

Agent:

- `POST /api/remote-claude/agent/sync`                    register + push session list
- `POST /api/remote-claude/agent/commands/claim`          claim next queued command
- `POST /api/remote-claude/agent/commands/{id}/result`    report completed/failed
- `POST /api/remote-claude/agent/events`                  push a stream-json event

Live (subscribe via the generic kernel events stream):

- `GET  /api/events/spaces/remote_claude.machine:{m}/stream`
- `GET  /api/events/spaces/remote_claude.session:{sid}/stream`

## Command types (claude_executor)

| Type | Purpose |
|---|---|
| `run.start`            | Spawn claude with first user message |
| `run.input`            | Append to a live run's stdin |
| `run.interrupt`        | SIGINT the live process |
| `session.delete`       | unlink the local jsonl + drop bridge row |
| `session.transcript`   | read jsonl from disk, return events |
| `fs.list` / `fs.mkdir` | directory ops on the PC |
| `approval.respond`     | (placeholder) |
