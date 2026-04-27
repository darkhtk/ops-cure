"""Kernel binding for remote_codex.

Surfaces two synthetic kernel space families that the state service
mirrors events into:

  - ``remote_codex.machine:{machine_id}``  - command lifecycle, machine
    status. Lets device-side runners subscribe via the kernel event channel
    instead of polling claim-next in a hot loop.
  - ``remote_codex.thread:{thread_id}``  - per-thread events (messages,
    state, task, snapshot). Lets browsers replace /api/remote-codex/.../live
    with the generic kernel events stream.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ...db import session_scope
from ...kernel.bindings import KernelBehaviorBinding
from ...kernel.spaces import SpaceSummary
from ...models import RemoteCodexMachineModel, RemoteCodexThreadModel
from .state_service import (
    REMOTE_CODEX_MACHINE_SPACE_PREFIX,
    REMOTE_CODEX_THREAD_SPACE_PREFIX,
)


class RemoteCodexKernelProvider:
    behavior_id = "remote_codex"

    def get_space(self, *, space_id: str) -> SpaceSummary | None:
        if space_id.startswith(REMOTE_CODEX_MACHINE_SPACE_PREFIX):
            return self._get_machine_space(space_id)
        if space_id.startswith(REMOTE_CODEX_THREAD_SPACE_PREFIX):
            return self._get_thread_space(space_id)
        return None

    def get_space_by_thread(self, *, thread_id: str) -> SpaceSummary | None:
        del thread_id  # legacy lookup, not used now that thread spaces are first-class
        return None

    # --- internals --------------------------------------------------------

    def _get_machine_space(self, space_id: str) -> SpaceSummary | None:
        machine_id = space_id[len(REMOTE_CODEX_MACHINE_SPACE_PREFIX):]
        if not machine_id:
            return None
        # Detach the row attributes while the session is still open. Reading
        # `row.display_name` (or any other column) after the `with` block
        # ends raises DetachedInstanceError because expire_on_commit closes
        # the row's identity map slot. Capture what we need into plain
        # locals first.
        display_name: str | None = None
        created_at = None
        last_seen_at = None
        found = False
        with session_scope() as db:
            row = db.scalar(
                select(RemoteCodexMachineModel).where(RemoteCodexMachineModel.machine_id == machine_id),
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
            domain_type="remote_codex.machine",
            transport_kind="kernel_event_stream",
            transport_address=machine_id,
            title=title,
            status="online" if found else "unknown",
            created_at=created_at or now,
            updated_at=last_seen_at or now,
            actors=[],
            metadata={"machine_id": machine_id},
        )

    def _get_thread_space(self, space_id: str) -> SpaceSummary | None:
        thread_id = space_id[len(REMOTE_CODEX_THREAD_SPACE_PREFIX):]
        if not thread_id:
            return None
        title: str | None = None
        cwd: str | None = None
        machine_id: str | None = None
        updated_at = None
        found = False
        with session_scope() as db:
            row = db.scalar(
                select(RemoteCodexThreadModel).where(RemoteCodexThreadModel.thread_id == thread_id)
            )
            if row is not None:
                found = True
                title = row.title
                cwd = row.cwd
                machine_id = row.machine_id
                updated_at_ms = getattr(row, "updated_at_ms", 0)
                if updated_at_ms:
                    updated_at = datetime.fromtimestamp(updated_at_ms / 1000.0, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        # Always return a Summary (even when row is gone) so subscribe is
        # accepted -- the kernel events stream itself doesn't need the row,
        # only the API endpoint does this lookup for 404/200 dispatch.
        return SpaceSummary(
            id=space_id,
            domain_type="remote_codex.thread",
            transport_kind="kernel_event_stream",
            transport_address=thread_id,
            title=title or thread_id,
            status="online" if found else "unknown",
            created_at=updated_at or now,
            updated_at=updated_at or now,
            actors=[],
            metadata={"thread_id": thread_id, "machine_id": machine_id, "cwd": cwd},
        )


def build_remote_codex_kernel_binding() -> KernelBehaviorBinding:
    provider = RemoteCodexKernelProvider()
    return KernelBehaviorBinding(
        behavior_id="remote_codex",
        space_provider=provider,
    )
