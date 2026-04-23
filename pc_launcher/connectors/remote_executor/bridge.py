from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ...bridge_client import BridgeClient


class RemoteExecutorBridge(Protocol):
    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
    ) -> dict[str, Any] | None: ...

    def claim_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_seconds: int = 90,
    ) -> dict[str, Any]: ...

    def heartbeat_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        phase: str,
        summary: str,
        lease_seconds: int = 90,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
    ) -> dict[str, Any]: ...

    def add_remote_task_evidence(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def add_remote_task_note(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        content: str,
    ) -> dict[str, Any]: ...

    def complete_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        summary: str,
    ) -> dict[str, Any]: ...

    def fail_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        error_text: str,
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class BridgeRemoteExecutorClient:
    bridge_client: BridgeClient

    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
    ) -> dict[str, Any] | None:
        return self.bridge_client.claim_next_remote_task_for_machine(
            machine_id=machine_id,
            actor_id=actor_id,
            lease_seconds=lease_seconds,
        )

    def claim_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_seconds: int = 90,
    ) -> dict[str, Any]:
        return self.bridge_client.claim_remote_task(
            task_id=task_id,
            actor_id=actor_id,
            lease_seconds=lease_seconds,
        )

    def heartbeat_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        phase: str,
        summary: str,
        lease_seconds: int = 90,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
    ) -> dict[str, Any]:
        return self.bridge_client.heartbeat_remote_task(
            task_id=task_id,
            actor_id=actor_id,
            lease_token=lease_token,
            phase=phase,
            summary=summary,
            lease_seconds=lease_seconds,
            commands_run_count=commands_run_count,
            files_read_count=files_read_count,
            files_modified_count=files_modified_count,
            tests_run_count=tests_run_count,
        )

    def add_remote_task_evidence(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.bridge_client.add_remote_task_evidence(
            task_id=task_id,
            actor_id=actor_id,
            kind=kind,
            summary=summary,
            payload=payload,
        )

    def add_remote_task_note(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        content: str,
    ) -> dict[str, Any]:
        return self.bridge_client.add_remote_task_note(
            task_id=task_id,
            actor_id=actor_id,
            kind=kind,
            content=content,
        )

    def complete_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        summary: str,
    ) -> dict[str, Any]:
        return self.bridge_client.complete_remote_task(
            task_id=task_id,
            actor_id=actor_id,
            lease_token=lease_token,
            summary=summary,
        )

    def fail_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        error_text: str,
    ) -> dict[str, Any]:
        return self.bridge_client.fail_remote_task(
            task_id=task_id,
            actor_id=actor_id,
            lease_token=lease_token,
            error_text=error_text,
        )
