# Ops-Cure Launcher

Ops-Cure Launcher is the execution plane. It stays on the Windows machine that has local AI CLIs installed.

Responsibilities:

- discovers preconfigured `project.yaml` files
- registers those project manifests outbound to the NAS bridge
- claims pending session launches from the bridge
- spawns one worker process per agent
- keeps workers polling the bridge for jobs and heartbeats

## Setup

1. Copy `.env.example` to `.env`.
2. Set `BRIDGE_TOKEN` to the same value used by the NAS bridge.
3. Adjust `CLAUDE_EXECUTABLE` / `CLAUDE_ARGS_JSON` and, if needed later, `CODEX_EXECUTABLE` so they match the installed CLI commands on this PC.
   The sample `.env` uses `--permission-mode bypassPermissions` so the local worker can run non-interactively without hanging on permission prompts.
4. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

5. Start the launcher daemon:

```bash
python launcher.py daemon --projects-dir .\projects
```

Ops-Cure now enforces a single launcher instance per `launcher_id` with a lock file under the projects directory.
For normal use, run it from Windows Task Scheduler `At startup` or `At logon` so the execution plane comes back automatically after reboot.

## BAT bootstrap

The included `scripts/start_project.bat` starts the launcher daemon without interactive setup.
It is suitable for Task Scheduler, Start Menu Startup, or a hidden auto-start wrapper.

## Local development mode

If you want to test the flow without real AI CLIs, change an agent `cli:` value to `mock`. The mock adapter echoes the prompt payload so you can test session, job, and transcript flow end to end.

## Current machine note

This Windows machine has a working `claude` CLI, so the sample project is configured to use Claude for all agents on first run. The Codex executable currently returns an access-denied error in this environment, so Codex is left optional until that local install is corrected.
