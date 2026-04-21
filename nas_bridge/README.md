# Opscure Bridge

The bridge is the control plane for Opscure.

It is responsible for:

- Discord slash commands and thread lifecycle
- canonical session, task, handoff, job, and transcript state
- ready queue scheduling and worker claim flow
- worker registration, heartbeat tracking, and recovery
- status rendering back into Discord threads

The bridge does **not** run local AI CLIs itself.
Those stay on the Windows execution plane.

If you want the higher-level overview first, see:

- [C:\Users\darkh\Projects\ops-cure\README.md](C:/Users/darkh/Projects/ops-cure/README.md)
- [C:\Users\darkh\Projects\ops-cure\docs\architecture.md](C:/Users/darkh/Projects/ops-cure/docs/architecture.md)

## What The Bridge Owns

The bridge is the source of truth for orchestration state.

Important state families include:

- `sessions`
- `agents`
- `jobs`
- `tasks`
- `handoffs`
- `task_events`
- `transcripts`
- `verification_runs`

It also owns coordination safety fields such as:

- `session_epoch`
- `task_revision`
- `lease_token`
- `idempotency_key`

These are used so that stale workers, duplicate callbacks, and recovery flows do not corrupt current state.

## Runtime Responsibilities

At runtime, the bridge:

1. receives Discord commands and thread messages
2. creates or resumes sessions
3. stores canonical tasks and handoffs
4. exposes ready work for workers to self-claim
5. records worker completions, failures, and verification outcomes
6. rebuilds thread-visible state from canonical data

The thread is an operator surface and async collaboration bus.
It is not the source of truth for scheduling.

## Local Run

1. Copy `.env.example` to `.env`.
2. Set the shared auth token and Discord settings.
3. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

4. Start the service:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

## Docker

Typical local Docker start:

```bash
docker compose up --build -d
```

The SQLite database is persisted under `./data/bridge.db`.

## API Surface

The bridge exposes API routes for:

- health checks
- session lookup and summary
- worker registration
- worker heartbeat
- job claim and completion
- verification flow

The exact endpoints live under:

```text
nas_bridge/app/api/
```

## Discord Command Surface

The bridge currently supports commands such as:

- `/project start`
- `/project find`
- `/project status`
- `/project pause`
- `/project resume`
- `/project close`
- `/project cleanup`
- `/agent restart`
- `/session reset`
- `/policy show`
- `/policy set`
- `/verify run`
- `/verify latest`
- `/verify approve`
- `/verify reject`

## Scheduling Model

Opscure is converging on canonical ready queues plus self-claim.

That means:

- the bridge decides which tasks are ready
- idle workers claim matching work
- the system does not depend entirely on one role manually pushing every next step

Scheduling decisions can consider:

- role match
- dependency readiness
- file scope
- semantic scope
- retry policy
- priority and aging

## Status Rendering

The bridge renders two kinds of thread output:

- status cards
- event messages

Visible message prefixes include:

- `OPS:`
- `ANSWER:`
- `HUMAN:`
- `ISSUE:`

Those are rendered from structured internal state and events.
They are not supposed to become the scheduling source of truth.

The status card also shows live worker activity:

- only workers that are currently `busy`
- only the latest activity line reported by each busy worker
- no cumulative per-line worker log output in the thread

This keeps the thread readable while still showing what active workers are doing right now.

## Recovery And Drift Handling

The bridge is also responsible for:

- launcher registration tracking
- worker heartbeat aging
- startup timeout handling
- orphan thread cleanup
- stale session recovery
- projection rebuild triggers

The goal is for thread state, canonical DB state, and local projections to converge again after interruptions.

## Synology Deployment Notes

This repository is commonly deployed to a Synology NAS using Docker.

Typical pattern:

1. copy `nas_bridge/` to the NAS
2. configure `.env`
3. run:

```bash
docker compose up -d --build
```

In the current setup, the bridge service is typically exposed externally on port `18080`, while the application still listens on container port `8080`.

## Local Development Mode

Set `BRIDGE_DISABLE_DISCORD=true` if you want to run the bridge API without connecting to Discord.

In that mode:

- thread creation is simulated
- outbound messages are logged instead of sent

This is useful when working on API, state, migration, or scheduling logic locally.

## Related Docs

- Root overview: [C:\Users\darkh\Projects\ops-cure\README.md](C:/Users/darkh/Projects/ops-cure/README.md)
- Architecture guide: [C:\Users\darkh\Projects\ops-cure\docs\architecture.md](C:/Users/darkh/Projects/ops-cure/docs/architecture.md)
- Launcher details: [C:\Users\darkh\Projects\ops-cure\pc_launcher\README.md](C:/Users/darkh/Projects/ops-cure/pc_launcher/README.md)
