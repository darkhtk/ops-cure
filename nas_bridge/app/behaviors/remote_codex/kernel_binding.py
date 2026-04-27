"""Kernel binding for remote_codex.

Today this surfaces just enough to let the generic /api/events/spaces/...
SSE endpoint accept the synthetic ``remote_codex.machine:{machine_id}``
space ids that the state service publishes command events to. That lets
device-side runners subscribe via the kernel event channel instead of
polling claim-next in a hot loop.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ...db import session_scope
from ...kernel.bindings import KernelBehaviorBinding
from ...kernel.spaces import SpaceSummary
from ...models import RemoteCodexMachineModel
from .state_service import REMOTE_CODEX_MACHINE_SPACE_PREFIX


class RemoteCodexKernelProvider:
    behavior_id = "remote_codex"

    def get_space(self, *, space_id: str) -> SpaceSummary | None:
        if not space_id.startswith(REMOTE_CODEX_MACHINE_SPACE_PREFIX):
            return None
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

    def get_space_by_thread(self, *, thread_id: str) -> SpaceSummary | None:
        # Threads aren't part of the synthetic machine space taxonomy —
        # remote_codex's per-thread events still flow through the
        # behavior-specific /api/remote-codex/.../live SSE pipe.
        return None


def build_remote_codex_kernel_binding() -> KernelBehaviorBinding:
    provider = RemoteCodexKernelProvider()
    return KernelBehaviorBinding(
        behavior_id="remote_codex",
        space_provider=provider,
    )
