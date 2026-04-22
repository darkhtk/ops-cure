# Opscure Checklists

This document captures the practical checklists and guardrails that reduce the most common operating mistakes in Opscure.

The recurring failure modes are usually not "hard architecture" failures. They are often execution mistakes around:

- PowerShell command syntax
- Windows encoding paths
- confusing canonical state with markdown projections
- trusting rendered status over actual jobs and heartbeats
- deploying before the bridge, launcher, and worker layers all agree

## Before Changing Code

Run this checklist before making framework changes.

- Confirm what the source of truth is for this problem.
  - Prefer canonical bridge state (`sessions`, `agents`, `jobs`, `tasks`, `handoffs`, `task_events`) over local markdown files.
- Check whether the issue is in:
  - canonical DB state
  - projection rendering
  - launcher or worker runtime
  - Discord rendering only
- Verify the actual process model before reasoning from names.
  - On Windows, confirm whether the running process is `launcher.py`, `cli_worker.py`, or another wrapper.
- Treat local markdown files as projections unless proven otherwise.
  - `CURRENT_STATE.md`, `CURRENT_TASK.md`, `HANDOFFS.md`, and `TASK_BOARD.md` can drift.
- If the issue involves Discord status:
  - compare status card output
  - active jobs in the DB
  - live worker heartbeats
- If the issue involves verifier or review failures:
  - separate framework failures from project-specific capability mismatches.

## Before Running Commands On Windows

Opscure is operated from PowerShell on Windows. Do not assume bash semantics.

- Do not use `&&` or bash-only shell patterns in PowerShell.
- For sequential commands, prefer separate calls or PowerShell separators such as `;`.
- Be careful with redirection and quoting.
  - `>` and pipes behave differently from bash in some cases.
- When embedding multi-line scripts, prefer PowerShell here-strings.
- When checking running processes, inspect the full command line, not just the executable name.

## Encoding Safety Rules

If the issue involves Korean, mojibake, logs, or CLI output, assume encoding can be involved.

- Do not assume UTF-8 is preserved just because the final file is written with UTF-8.
- Check the full path:
  - CLI output generation
  - subprocess capture
  - bridge transport
  - markdown write
- On Windows, treat PowerShell output and console code page as suspect until verified.
- Prefer explicit UTF-8 handling for subprocess output.
- Keep `stdout.bin` and `stderr.bin` artifacts when debugging hard encoding failures.
- If a string looks corrupted in markdown, verify whether the corruption happened:
  - before capture
  - during decode
  - during render

## Before Trusting Session State

Use this checklist whenever a session looks inconsistent.

- Check whether there is an open session in canonical DB state.
- Check whether there are `in_progress` jobs.
- Check whether workers are actually attached or merely registered.
- Distinguish:
  - OS process count
  - bridge attachment count
  - active heartbeat count
- If the status card says `busy`, confirm that the DB also has an `in_progress` job.
- If the DB says a task is active but markdown says otherwise, trust the DB first.
- If markdown says work remains but the session is settled, treat projection drift as likely.

## Before Deploying

Use this checklist before pushing a bridge or launcher deployment.

- Confirm the modified files are the intended ones.
- Compile the Python modules you changed.
- Confirm whether the change affects:
  - bridge only
  - launcher only
  - both
- If the change touches worker reporting, attachment, or status rendering:
  - verify the bridge layer
  - verify the launcher layer
  - verify live health after restart
- If the change touches Discord rendering:
  - confirm whether it updates plain text, embed rendering, or both
- If the change touches canonical state:
  - verify migrations, snapshots, and projection rebuild behavior
- If the change touches verifier or review flow:
  - check capability mismatch risk before blaming project code

## Deployment Checklist

Use this when actually shipping a change.

### 1. Git

- Stage only the intended files.
- Commit with a message that reflects the real operational change.
- Push the current branch.

### 2. NAS Bridge

- Copy the updated `nas_bridge/` contents to the NAS live directory.
- Preserve runtime data such as `.env` and `data/bridge.db`.
- Rebuild and restart the bridge container.
- Confirm `/healthz` from the LAN endpoint.

### 3. Local Launcher

- Restart the local launcher if the change affects:
  - worker runtime
  - prompts
  - launcher registration
  - bridge client behavior
- Confirm only the intended launcher instance is running.
- Confirm worker processes match the expected session state.

### 4. Post-Deploy Verification

- Check health:
  - `status`
  - `discord_connected`
  - `active_launchers`
  - `tracked_projects`
- If there is a live session:
  - compare worker processes
  - compare attached workers
  - compare active jobs
  - compare status card output
- If there is no live session:
  - confirm launcher registration is still healthy

## Common Mistakes To Avoid

These are the mistakes most worth actively checking for.

- Using bash syntax in PowerShell.
- Trusting rendered markdown over canonical state.
- Trusting `worker_id` alone over recent heartbeat data.
- Assuming a process name tells the whole truth.
- Letting thread-rendered or truncated strings leak into durable task records.
- Treating project-specific verifier mismatch as a framework crash.
- Declaring deployment complete before checking:
  - bridge rebuild
  - launcher restart
  - health
  - registration

## Decision Order

When diagnosing a live problem, use this order:

1. Canonical DB state
2. Active jobs and agent heartbeats
3. Session status card
4. Thread transcript and recent delta
5. Local markdown projections

If levels 1 and 5 disagree, treat level 5 as stale until proven otherwise.
