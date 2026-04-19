# Ops-Cure

Ops-Cure is a Discord-native local agent orchestration framework built on a two-plane architecture:

- `nas_bridge/` is the Ops-Cure Bridge: the always-on control plane for Discord, SQLite state, thread routing, worker registration, heartbeats, jobs, and transcripts.
- `pc_launcher/` is the Ops-Cure Launcher: the Windows execution plane that reads YAML project configs, registers them with the bridge, launches one worker per agent, and runs whitelisted CLI adapters as subprocesses.
- Discord thread ids are the bridge session key. Local CLIs only receive an opaque `session_id`.

## Implementation plan

1. Bring up the NAS bridge with FastAPI, Discord slash commands, SQLite models, and secure worker APIs.
2. Add launcher-driven project manifest registration so the bridge only accepts preconfigured YAML projects.
3. Add outbound-only Windows polling for launcher session claims and worker job pulls.
4. Wrap Codex and Claude behind fixed adapter classes so Discord messages never become raw shell commands.
5. Persist session, agent, job, and transcript state in SQLite and surface the lifecycle in Discord thread messages.
6. Ship Docker for the NAS side, sample YAML and prompts for the PC side, plus setup docs and local development mode.

## Repository tree

```text
repo/
  README.md
  nas_bridge/
    .env.example
    Dockerfile
    README.md
    docker-compose.yml
    requirements.txt
    data/
    app/
      __init__.py
      auth.py
      command_router.py
      config.py
      db.py
      discord_gateway.py
      main.py
      message_router.py
      models.py
      schemas.py
      session_service.py
      thread_manager.py
      transcript_service.py
      worker_registry.py
      api/
        __init__.py
        health.py
        sessions.py
        workers.py
  pc_launcher/
    .env.example
    README.md
    __init__.py
    bridge_client.py
    cli_adapters.py
    cli_worker.py
    config_loader.py
    launcher.py
    requirements.txt
    worker_runtime.py
    scripts/
      start_project.bat
    projects/
      sample_project/
        project.yaml
        prompts/
          coder.md
          planner.md
          reviewer.md
```

## SQLite schema

### `sessions`

- `id` TEXT PRIMARY KEY
- `project_name` TEXT NOT NULL
- `preset` TEXT NULL
- `discord_thread_id` TEXT UNIQUE NOT NULL
- `guild_id` TEXT NOT NULL
- `parent_channel_id` TEXT NOT NULL
- `workdir` TEXT NOT NULL
- `status` TEXT NOT NULL
- `created_by` TEXT NOT NULL
- `launcher_id` TEXT NULL
- `send_ready_message` BOOLEAN NOT NULL
- `created_at` TIMESTAMP NOT NULL
- `closed_at` TIMESTAMP NULL

### `agents`

- `id` TEXT PRIMARY KEY
- `session_id` TEXT NOT NULL REFERENCES `sessions(id)`
- `agent_name` TEXT NOT NULL
- `cli_type` TEXT NOT NULL
- `role` TEXT NOT NULL
- `is_default` BOOLEAN NOT NULL
- `status` TEXT NOT NULL
- `last_heartbeat_at` TIMESTAMP NULL
- `pid_hint` INTEGER NULL
- `worker_id` TEXT NULL
- `last_error` TEXT NULL

### `jobs`

- `id` TEXT PRIMARY KEY
- `session_id` TEXT NOT NULL REFERENCES `sessions(id)`
- `agent_name` TEXT NOT NULL
- `job_type` TEXT NOT NULL
- `source_discord_message_id` TEXT NULL
- `user_id` TEXT NOT NULL
- `input_text` TEXT NOT NULL
- `status` TEXT NOT NULL
- `worker_id` TEXT NULL
- `result_text` TEXT NULL
- `error_text` TEXT NULL
- `created_at` TIMESTAMP NOT NULL
- `claimed_at` TIMESTAMP NULL
- `completed_at` TIMESTAMP NULL

### `transcripts`

- `id` TEXT PRIMARY KEY
- `session_id` TEXT NOT NULL REFERENCES `sessions(id)`
- `direction` TEXT NOT NULL
- `actor` TEXT NOT NULL
- `content` TEXT NOT NULL
- `source_discord_message_id` TEXT NULL
- `created_at` TIMESTAMP NOT NULL

## Core flow

1. The Windows launcher scans `project.yaml` files and registers project manifests to the bridge.
2. A Discord user runs `/project start name:<session-name> preset:<optional-preset>`.
3. The bridge resolves the preset from registered YAML manifests, validates guild, channel, and allowed user ids, creates a SQLite session row, and opens a Discord thread using the user-provided session name.
4. The launcher claims the pending launch and spawns one worker process per configured agent.
5. Workers register, heartbeat, and pull jobs from the bridge.
6. Thread messages route by `@agentname` prefix, or auto-route only when a single agent exists.
7. Worker results are sanitized, stored in transcripts, and posted back into the same Discord thread.
