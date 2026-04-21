# Opscure Launcher

The launcher is the Windows-side execution plane for Opscure.

It is responsible for:

- discovering registered execution profiles
- registering those profiles with the bridge
- supervising one local worker process per role
- running local AI CLI jobs
- running verification commands
- writing local artifacts and markdown projections

If you want the system-level view first, start here:

- [C:\Users\darkh\Projects\ops-cure\README.md](C:/Users/darkh/Projects/ops-cure/README.md)
- [C:\Users\darkh\Projects\ops-cure\docs\architecture.md](C:/Users/darkh/Projects/ops-cure/docs/architecture.md)

## Current Runtime Model

The default sample profile currently defines five roles:

- `planner`
- `curator`
- `coder`
- `verifier`
- `reviewer`

The launcher supervises one worker process per configured agent when a session is active.

While a worker is busy, the launcher also tracks the latest stdout or stderr line coming from the local CLI process and reports only that latest line back to the bridge.

## What The Launcher Does

At runtime, the launcher:

1. loads `project.yaml` profiles from `projects/`
2. registers those profiles outbound to the bridge
3. claims session launches and ready work
4. starts local worker processes
5. keeps workers polling the bridge for jobs and heartbeats
6. runs verification commands and stores evidence locally

The launcher does **not** own orchestration truth.
The bridge remains the source of truth for sessions, tasks, handoffs, jobs, and task events.

## Profile Layout

Execution profiles live under:

```text
pc_launcher/projects/<profile-name>/
  project.yaml
  prompts/
    planner.md
    curator.md
    coder.md
    verifier.md
    reviewer.md
    finder.md
```

The sample profile currently includes:

- `profile_name: sample`
- top-level project finder roots under `C:\Users\darkh\Projects`
- Discord thread naming rules
- bridge connection settings
- agent definitions
- policy defaults
- verification modes

The long-term goal is for project-specific behavior to stay inside:

- `project.yaml`
- prompt files
- local wrapper scripts or command lines

## Setup

1. Copy `.env.example` to `.env`.
2. Set `BRIDGE_TOKEN` to the same shared token used by the bridge.
3. Point CLI executable settings at the actual local commands installed on this PC.
4. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Start The Launcher

Typical local start:

```bash
python launcher.py daemon --projects-dir .\projects
```

The launcher is designed to run continuously in the background.

Recommended production setup:

- run it from Windows Task Scheduler
- start `At startup` or `At logon`
- keep the configured `launcher_id` stable

## Single-Instance Protection

Opscure enforces one launcher instance per `launcher_id`.

A lock file is created under the projects directory so that:

- duplicate launcher processes do not fight over sessions
- the bridge sees one stable execution target

## Worker Model

The launcher supervises local worker processes that run:

- `planner`: request interpretation and decomposition
- `curator`: flow cleanup and projection hygiene
- `coder`: implementation
- `verifier`: build, run, capture, and evidence generation
- `reviewer`: evidence-based approval or replan decisions

Workers poll the bridge for jobs.
The bridge decides canonical ready state; workers do not invent orchestration truth locally.

When a worker is busy, its latest CLI activity line is also sent in heartbeats so the bridge can show a current "what is this worker doing now?" view in the Discord status card.

## Verification Lane

The launcher also owns local verification execution.

Expected verification outputs include:

- `stdout.log`
- `stderr.log`
- `stdout.bin`
- `stderr.bin`
- `result.json`
- screenshots such as `desktop.png`

Verification commands are configured per profile in `project.yaml`.
The current sample profile uses generic placeholder commands until replaced by project-specific scripts.

## Local Artifacts

The launcher writes local artifacts under the configured session directory, typically `_discord_sessions/` inside the project root.

Examples:

- `CURRENT_STATE.md`
- `CURRENT_TASK.md`
- `HANDOFFS.md`
- `TASK_BOARD.md`
- task cards under `TASKS/`
- run logs under `RUN_LOGS/`
- verification outputs under `_verification/`

These files are useful for debugging and inspection, but they are projections of bridge state, not the scheduler's source of truth.

## Local Development Mode

If you want to exercise the flow without real AI CLIs, switch an agent `cli:` value to `mock`.

That lets you validate:

- profile loading
- session start
- job claim
- transcript flow
- projection rebuild

without depending on a live local CLI.

## Current Machine Note

The sample profile currently uses Claude for all roles because that CLI is known to work in this Windows environment.

Codex can still be wired in later, but it is intentionally optional until the local installation is confirmed stable.

## Related Docs

- Root overview: [C:\Users\darkh\Projects\ops-cure\README.md](C:/Users/darkh/Projects/ops-cure/README.md)
- Architecture guide: [C:\Users\darkh\Projects\ops-cure\docs\architecture.md](C:/Users/darkh/Projects/ops-cure/docs/architecture.md)
- Bridge details: [C:\Users\darkh\Projects\ops-cure\nas_bridge\README.md](C:/Users/darkh/Projects/ops-cure/nas_bridge/README.md)
