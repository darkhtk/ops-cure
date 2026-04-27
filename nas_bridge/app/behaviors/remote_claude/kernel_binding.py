"""Kernel binding for remote_claude.

Surfaces the synthetic kernel space ids that the state service publishes
events to:

  - ``remote_claude.machine:{machine_id}``  - machine-scoped commands /
    session list / machine status events.
  - ``remote_claude.session:{session_id}``  - per-session stream-json events
    from the agent (claude.event / claude.stderr / claude.exit / ...).

Lets browsers + device-side runners subscribe via the generic
``/api/events/spaces/{space_id}/stream`` SSE channel instead of the legacy
behavior-specific ``/api/remote-claude/.../live`` pipes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ...db import session_scope
from ...kernel.bindings import KernelBehaviorBinding
from ...kernel.spaces import SpaceSummary
from ...models import RemoteClaudeMachineModel, RemoteClaudeSessionModel
from .state_service import (
    REMOTE_CLAUDE_MACHINE_SPACE_PREFIX,
    REMOTE_CLAUDE_SESSION_SPACE_PREFIX,
)


class RemoteClaudeKernelProvider:
    behavior_id = "remote_claude"

    def get_space(self, *, space_id: str) -> SpaceSummary | None:
        if space_id.startswith(REMOTE_CLAUDE_MACHINE_SPACE_PREFIX):
            return self._get_machine_space(space_id)
        if space_id.startswith(REMOTE_CLAUDE_SESSION_SPACE_PREFIX):
            return self._get_session_space(space_id)
        return None

    def get_space_by_thread(self, *, thread_id: str) -> SpaceSummary | None:
        del thread_id  # remote_claude doesn't expose threads in this taxonomy
        return None

    # --- internals --------------------------------------------------------

    def _get_machine_space(self, space_id: str) -> SpaceSummary | None:
        machine_id = space_id[len(REMOTE_CLAUDE_MACHINE_SPACE_PREFIX):]
        if not machine_id:
            return None
        # Detach DB row attributes inside the with-block; reading after
        # session close raises DetachedInstanceError (same gotcha codex hit).
        display_name: str | None = None
        created_at = None
        last_seen_at = None
        found = False
        with session_scope() as db:
            row = db.scalar(
                select(RemoteClaudeMachineModel).where(
                    RemoteClaudeMachineModel.machine_id == machine_id
                )
            )
            if row is not None:
                found = True
                display_name = row.display_name
                created_at = getattr(row, "created_at", None)
                last_seen_at = getattr(row, "last_seen_at", None)
        title = display_name or machine_id
        now = datetime.now(timezone.utc)
        return SpaceSummary(
            id=space_id,
            domain_type="remote_claude.machine",
            transport_kind="kernel_event_stream",
            transport_address=machine_id,
            title=title,
            status="online" if found else "unknown",
            created_at=created_at or now,
            updated_at=last_seen_at or now,
            actors=[],
            metadata={"machine_id": machine_id},
        )

    def _get_session_space(self, space_id: str) -> SpaceSummary | None:
        session_id = space_id[len(REMOTE_CLAUDE_SESSION_SPACE_PREFIX):]
        if not session_id:
            return None
        title: str | None = None
        cwd: str | None = None
        machine_id: str | None = None
        created_at = None
        updated_at = None
        found = False
        with session_scope() as db:
            row = db.scalar(
                select(RemoteClaudeSessionModel).where(
                    RemoteClaudeSessionModel.session_id == session_id
                )
            )
            if row is not None:
                found = True
                title = row.title
                cwd = row.cwd
                machine_id = row.machine_id
                created_at = getattr(row, "created_at", None)
                updated_at = getattr(row, "updated_at", None)
        # If the row is gone (deleted / never synced yet) we still return a
        # synthetic Summary so the events stream endpoint accepts the
        # subscribe — the actual events flow regardless of DB state.
        now = datetime.now(timezone.utc)
        return SpaceSummary(
            id=space_id,
            domain_type="remote_claude.session",
            transport_kind="kernel_event_stream",
            transport_address=session_id,
            title=title or session_id,
            status="online" if found else "unknown",
            created_at=created_at or now,
            updated_at=updated_at or now,
            actors=[],
            metadata={"session_id": session_id, "machine_id": machine_id, "cwd": cwd},
        )


def build_remote_claude_kernel_binding() -> KernelBehaviorBinding:
    provider = RemoteClaudeKernelProvider()
    return KernelBehaviorBinding(
        behavior_id="remote_claude",
        space_provider=provider,
    )
