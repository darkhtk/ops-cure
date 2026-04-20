from __future__ import annotations

from .base import ExecutionProvider, ExecutionStatus, ExecutionTarget
from ...worker_registry import WorkerRegistry


class WindowsLauncherExecutionProvider(ExecutionProvider):
    provider_name = "windows_launcher"

    def __init__(self, registry: WorkerRegistry) -> None:
        self._registry = registry

    def status_for_project(self, *, project_name: str, target: ExecutionTarget) -> ExecutionStatus:
        record = self._registry.find_launcher_for_project(project_name)
        if record is None:
            return ExecutionStatus(
                state="offline",
                detail=(
                    "No active launcher is registered for this project. "
                    "Ops-Cure is waiting for the Windows launcher auto-start path to reconnect."
                ),
            )
        if target.launcher_id_hint and record.launcher_id != target.launcher_id_hint:
            return ExecutionStatus(
                state="degraded",
                launcher_id=record.launcher_id,
                detail=(
                    f"Launcher `{record.launcher_id}` is online, but it does not match the "
                    f"configured hint `{target.launcher_id_hint}`."
                ),
            )
        return ExecutionStatus(
            state="online",
            launcher_id=record.launcher_id,
            detail=f"Launcher `{record.launcher_id}` is online for project `{project_name}`.",
        )

    def request_start(self, *, project_name: str, target: ExecutionTarget) -> ExecutionStatus:
        status = self.status_for_project(project_name=project_name, target=target)
        if status.state == "online":
            return status
        if target.auto_start_expected:
            return ExecutionStatus(
                state="awaiting_launcher",
                detail=(
                    "Launcher is offline. Ops-Cure expects Windows auto-start to bring it back "
                    "and will keep the session in recovery until then."
                ),
            )
        return status
