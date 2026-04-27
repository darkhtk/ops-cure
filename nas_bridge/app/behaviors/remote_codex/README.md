# `remote_codex` Behavior

This directory is the landing zone for the future `remote_codex` behavior.
It now contains a safe scaffold:

- `service.py`
- `schemas.py`
- `api.py`
- `kernel_binding.py`
- `discord_binding.py`

The ownership split is intentional:

- `Opscure` owns the `remote_codex` behavior and its canonical state.
- `codex-remote` owns the browser site that renders and drives that behavior.
- device-side executors remain adapter/runtime code, not kernel code.
- Discord remains a coordination mirror, not the canonical execution surface.

## What This Behavior Will Own

The future `remote_codex` behavior should own:

- machine and canonical thread bindings
- remote work objects and assignment state
- heartbeat and evidence state
- approval, interrupt, and stall state
- typed read models that the browser can trust
- projection of behavior facts into generic kernel `Space / Actor / Event` views

## What This Behavior Will Not Own

This behavior should not own:

- browser layout, interaction copy, or mobile-specific UX
- Codex app-server process control details
- Windows UTF-8 handling
- Discord room etiquette or message-tagging rules
- site-specific optimistic rendering details

Those belong in adapters:

- browser site adapter in `codex-remote`
- runtime/executor adapters in `pc_launcher`
- Discord coordination mirror adapters in `Opscure`

## Transition Status

Today, the remote execution flow still lives across product/service files and browser code.
This package exists so that once the remote contracts stabilize, the logic can be folded into a single behavior instead of staying spread across:

- bridge/product services
- browser-only state models
- runtime-specific execution glue

## Intended Package Shape

The current scaffold is intentionally thin. It wraps the existing product-layer
remote task service. The behavior API surface can be mounted safely as an alias
without changing the existing `/api/remote/...` product endpoints.

When the migration is ready, this package should grow toward:

```text
remote_codex/
  __init__.py
  api.py
  discord_binding.py
  kernel_binding.py
  schemas.py
  service.py
```

## Current Rule

Until that migration lands:

- put canonical remote execution truth in `Opscure`
- keep the browser site in `codex-remote`
- do not let the site become the canonical owner of task/evidence/approval state
- keep the current product-layer remote task service as the implementation body, and let this package become the stable behavior-facing facade

## Event transport — kernel subscription broker

`state_service._mirror_to_kernel_broker` publishes every behavior-local
event onto the kernel `subscription_broker`. Subscribers (browser + agent)
use the generic `/api/events/spaces/{space_id}/stream` SSE channel that
`chat`, `ops`, and `remote_claude` also use -- one transport, four
behaviors.

Synthetic spaces:

| Space id | Carries |
|---|---|
| `remote_codex.machine:{machine_id}` | command lifecycle (`remote_codex.command.queued/running/completed/failed`), machine status |
| `remote_codex.thread:{thread_id}`   | per-thread events (`remote_codex.messages`, `remote_codex.state`, `remote_codex.task`, `remote_codex.snapshot`, ...) |

Each `EventEnvelope.event.content` is the JSON-serialized legacy payload
(`{"kind": "command", "command": {...}}` etc.) so existing client-side
dispatch logic still works -- the only frontend change is to subscribe to
the kernel events stream instead of the (removed) behavior-specific
`/threads/{t}/live` and `/machines/{m}/live` SSE endpoints.

Phase status: command + machine + per-thread events all mirror. Subscribe-
side migration on `codex-remote` complete (commit `6c25629`).
