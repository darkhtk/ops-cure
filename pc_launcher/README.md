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

## Adding a PC as a remote machine (claude-remote / codex-remote sites)

To make a PC's disk + claude/codex CLI usable from the browser sites
(claude-remote, codex-remote), run the per-connector executor agent on
that PC. The agent registers itself with the NAS bridge as a "machine"
and the new hostname appears in the browser sidebar within ~30 s.

There are two parallel connectors under `pc_launcher/connectors/`:

| Connector | Browser site | Command type |
|---|---|---|
| `claude_executor` | claude-remote | `run.start` (claude --print stream-json) |
| `remote_executor` | codex-remote  | `turn.start` (codex app-server) |

Each ships three small batch scripts:

- `install.bat` — interactive setup. Asks for bridge URL, bearer token, and
  machine id; writes `.env` (or patches `pc_launcher/.env` + `project.yaml`
  for `remote_executor`).
- `start.bat` — launches the agent against the configured bridge. Logs to
  `_runtime/ops-cure/logs/<connector>.log`.
- `register-task.bat` — registers a Task Scheduler entry that runs
  `start.bat` on user logon. Idempotent (delete-then-create). Uses
  PowerShell's `Register-ScheduledTask` so it works without admin /
  without `schtasks.exe` quirks on non-interactive sessions.

Quick path for a fresh PC:

```cmd
git clone <ops-cure repo> %USERPROFILE%\Projects\ops-cure
cd %USERPROFILE%\Projects\ops-cure
python -m pip install -r requirements.txt

REM claude
cd pc_launcher\connectors\claude_executor
install.bat
register-task.bat

REM codex (in another shell, or after the above)
cd ..\remote_executor
install.bat
register-task.bat
```

Verify in the browser sidebar -- the new hostname should show up. If not,
check the agent log file under `_runtime/ops-cure/logs/`.

## Lower-level behavior tools (manual / scripting)

The `behavior_tools` CLI is the underlying mechanism the install scripts
above wrap. You normally don't need to call it directly.

```bash
python -m pc_launcher.behavior_tools install chat-participant
python -m pc_launcher.behavior_tools doctor chat-participant
python -m pc_launcher.behavior_tools run chat-participant --thread-id <thread_id> --actor-name <actor_name> --codex-thread-id <codex_thread_id>
python -m pc_launcher.behavior_tools install remote-executor
python -m pc_launcher.behavior_tools doctor remote-executor
python -m pc_launcher.behavior_tools run remote-executor --machine-id <machine_id> --actor-id <actor_id> --codex-thread-id <codex_thread_id>
```

For manual UTF-8-safe smoke messages on Windows:

```bash
python -m pc_launcher.behavior_tools send chat-participant --thread-id <thread_id> --actor-name <actor_name> --message-file C:\path\to\message.txt
```

Equivalent PowerShell wrappers are available:

- `pc_launcher/scripts/install_behavior.ps1`
- `pc_launcher/scripts/doctor_behavior.ps1`
- `pc_launcher/scripts/run_behavior.ps1`
- `pc_launcher/scripts/send_behavior_message.ps1`

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

The launcher writes local artifacts under the configured session directory. The current recommended location is outside the repo under `C:\Users\darkh\Projects\_runtime\ops-cure\discord-sessions\`.

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
