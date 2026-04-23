from __future__ import annotations

from pc_launcher.bridge_client import BridgeClient, BridgeClientError


class RecordingBridgeClient(BridgeClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _post(self, path: str, payload: dict[str, object]):  # type: ignore[override]
        self.calls.append((path, payload))
        if path in {
            "/api/remote-codex/tasks/task-1/heartbeat",
            "/api/remote-codex/tasks/task-1/evidence",
            "/api/remote-codex/tasks/task-1/complete",
            "/api/remote-codex/tasks/task-1/fail",
        }:
            raise BridgeClientError(f"{path} -> 404: {{'detail': 'Not Found'}}")
        return {"ok": True, "path": path, "payload": payload}


def test_remote_task_browser_surface_falls_back_to_agent_surface_on_missing_routes() -> None:
    client = RecordingBridgeClient()

    heartbeat = client.heartbeat_remote_task(
        task_id="task-1",
        actor_id="machine-a",
        lease_token="lease-1",
        phase="executing",
        summary="Doing work.",
        commands_run_count=1,
        files_read_count=2,
        files_modified_count=3,
        tests_run_count=4,
    )
    evidence = client.add_remote_task_evidence(
        task_id="task-1",
        actor_id="machine-a",
        kind="command_execution",
        summary="Ran a command.",
        payload={"commandId": "cmd-1"},
    )
    completed = client.complete_remote_task(
        task_id="task-1",
        actor_id="machine-a",
        lease_token="lease-1",
        summary="Done.",
    )
    failed = client.fail_remote_task(
        task_id="task-1",
        actor_id="machine-a",
        lease_token="lease-1",
        error_text="Boom.",
    )

    assert heartbeat["path"] == "/api/remote-codex/agent/tasks/task-1/heartbeat"
    assert evidence["path"] == "/api/remote-codex/agent/tasks/task-1/evidence"
    assert completed["path"] == "/api/remote-codex/agent/tasks/task-1/complete"
    assert failed["path"] == "/api/remote-codex/agent/tasks/task-1/fail"
    assert client.calls == [
        (
            "/api/remote-codex/tasks/task-1/heartbeat",
            {
                "actor_id": "machine-a",
                "lease_token": "lease-1",
                "phase": "executing",
                "summary": "Doing work.",
                "lease_seconds": 90,
                "commands_run_count": 1,
                "files_read_count": 2,
                "files_modified_count": 3,
                "tests_run_count": 4,
            },
        ),
        (
            "/api/remote-codex/agent/tasks/task-1/heartbeat",
            {
                "actorId": "machine-a",
                "phase": "executing",
                "summary": "Doing work.",
                "commandsRunCount": 1,
                "filesReadCount": 2,
                "filesModifiedCount": 3,
                "testsRunCount": 4,
            },
        ),
        (
            "/api/remote-codex/tasks/task-1/evidence",
            {
                "actor_id": "machine-a",
                "kind": "command_execution",
                "summary": "Ran a command.",
                "payload": {"commandId": "cmd-1"},
            },
        ),
        (
            "/api/remote-codex/agent/tasks/task-1/evidence",
            {
                "actorId": "machine-a",
                "kind": "command_execution",
                "summary": "Ran a command.",
                "payload": {"commandId": "cmd-1"},
            },
        ),
        (
            "/api/remote-codex/tasks/task-1/complete",
            {
                "actor_id": "machine-a",
                "lease_token": "lease-1",
                "summary": "Done.",
            },
        ),
        (
            "/api/remote-codex/agent/tasks/task-1/complete",
            {
                "actorId": "machine-a",
                "summary": "Done.",
            },
        ),
        (
            "/api/remote-codex/tasks/task-1/fail",
            {
                "actor_id": "machine-a",
                "lease_token": "lease-1",
                "error_text": "Boom.",
            },
        ),
        (
            "/api/remote-codex/agent/tasks/task-1/fail",
            {
                "actorId": "machine-a",
                "error": {"message": "Boom."},
            },
        ),
    ]
