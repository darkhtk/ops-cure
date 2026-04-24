# Workspace Layout

This document fixes the intended folder boundaries around the canonical repos.

## Canonical repositories

Keep only these repos as active source-of-truth workspaces under `C:\Users\darkh\Projects\`:

- `C:\Users\darkh\Projects\ops-cure`
- `C:\Users\darkh\Projects\codex-remote`

Everything else should be treated as runtime output, archived snapshots, or disposable worktrees.

## Top-level support folders

Use these top-level folders next to the canonical repos:

```text
C:\Users\darkh\Projects\
  ops-cure\
  codex-remote\
  _archive\
    codex-remote\
  _runtime\
    ops-cure\
    codex-remote\
  _worktrees\
    codex-remote\
```

## What belongs in `_archive`

Archived clone snapshots and one-off rescue copies:

- `codex-remote-bootstrap-backup-*`
- `codex-remote-bridge-fix`
- `codex-remote-upstream`

Do not keep these beside the canonical repos once they stop being the active workspace.

## What belongs in `_runtime`

Runtime output, screenshots, local logs, extracted app bundles, and local secrets.

### `ops-cure`

Recommended runtime paths:

- `C:\Users\darkh\Projects\_runtime\ops-cure\discord-sessions\`
- `C:\Users\darkh\Projects\_runtime\ops-cure\app-server-schema\`
- `C:\Users\darkh\Projects\_runtime\ops-cure\config\synology_ssh_credentials.env`

### `codex-remote`

Recommended runtime paths:

- `C:\Users\darkh\Projects\_runtime\codex-remote\playwright\`
- `C:\Users\darkh\Projects\_runtime\codex-remote\misc\`
- `C:\Users\darkh\Projects\_runtime\codex-remote\secrets\`

## What belongs in `_worktrees`

Parallel branch workspaces created from the canonical repo by `git worktree`.

Preferred pattern:

```powershell
git -C C:\Users\darkh\Projects\codex-remote worktree add C:\Users\darkh\Projects\_worktrees\codex-remote\bridge-fix codex/bridge-fix
```

Use worktrees instead of creating more top-level clone folders.

## Repo-level rules

- Keep runtime state out of the repo root.
- Keep secrets out of the repo root.
- Keep deploy copies conceptually separate from source repos.
- Prefer absolute documented runtime paths over ad hoc temp folders.
- Prefer `git worktree` over extra sibling clones.
