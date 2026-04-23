from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


class StubRemoteCodexService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        now = datetime.now(timezone.utc).isoformat()
        return {
            "task": {
                "taskId": task_id,
                "machineId": "machine-a",
                "threadId": "thread-a",
                "objective": "Smoke test remote_codex router.",
                "status": "queued",
                "sourceSurface": "browser",
                "createdAt": now,
                "updatedAt": now,
                "currentClaim": None,
                "latestHeartbeat": None,
                "recentEvidence": [],
                "latestApproval": None,
            }
        }


def test_remote_codex_router_uses_behavior_prefix(app_env) -> None:
    from app.behaviors.remote_codex.api import router

    service = StubRemoteCodexService()
    app = FastAPI()
    app.state.services = SimpleNamespace(remote_codex_service=service)
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/remote-codex/tasks/task-1",
        headers={"Authorization": "Bearer test-token"},
    )

    assert router.prefix == "/api/remote-codex"
    assert response.status_code == 200
    assert response.json()["task"]["taskId"] == "task-1"


def test_remote_codex_browser_and_agent_surface_round_trip(app_env) -> None:
    from app.main import app
    now = datetime.now(timezone.utc).isoformat()

    machine_payload = {
        "machine": {
            "machineId": "machine-z",
            "displayName": "Machine Z",
            "source": "agent",
            "activeTransport": "filesystem-storage",
            "runtimeMode": "standalone",
            "runtimeAvailable": True,
            "capabilities": {"liveControl": True},
            "lastSeenAt": now,
            "lastSyncAt": now,
        },
        "threads": [
            {
                "id": "thread-z",
                "title": "Remote Codex Thread",
                "cwd": "C:/repo",
                "rolloutPath": "C:/repo/.codex/rollout.jsonl",
                "updatedAtMs": 1700000000000,
                "createdAtMs": 1699999999000,
                "source": "app-server",
                "modelProvider": "openai",
                "model": "gpt-5.4",
                "reasoningEffort": "medium",
                "cliVersion": "1.0.0",
                "firstUserMessage": "Ship this UX",
                "status": {"type": "notLoaded"},
            }
        ],
        "snapshots": [
            {
                "thread": {
                    "id": "thread-z",
                    "title": "Remote Codex Thread",
                    "cwd": "C:/repo",
                    "rolloutPath": "C:/repo/.codex/rollout.jsonl",
                    "updatedAtMs": 1700000000000,
                    "createdAtMs": 1699999999000,
                    "source": "app-server",
                    "modelProvider": "openai",
                    "model": "gpt-5.4",
                    "reasoningEffort": "medium",
                    "cliVersion": "1.0.0",
                    "firstUserMessage": "Ship this UX",
                    "status": {"type": "notLoaded"},
                },
                "messages": [
                    {
                        "lineNumber": 1,
                        "timestamp": "2026-04-23T00:00:00+00:00",
                        "role": "user",
                        "phase": None,
                        "text": "Ship this UX",
                        "images": [
                            {
                                "src": "data:image/png;base64,abc123",
                                "alt": "Uploaded image 1",
                                "title": None,
                            }
                        ],
                    },
                    {
                        "lineNumber": 2,
                        "timestamp": "2026-04-23T00:00:01+00:00",
                        "role": "assistant",
                        "phase": "completed",
                        "text": "Working on it.",
                        "images": [],
                    },
                ],
                "totalMessages": 2,
                "lineCount": 2,
                "fileSize": 128,
                "syncedAt": "2026-04-23T00:00:02+00:00",
            }
        ],
    }

    with TestClient(app) as client:
        sync_response = client.post(
            "/api/remote-codex/agent/sync",
            headers={"Authorization": "Bearer test-token"},
            json=machine_payload,
        )
        assert sync_response.status_code == 200
        assert sync_response.json()["machine"]["machineId"] == "machine-z"

        health_response = client.get("/api/remote-codex/health", headers={"Authorization": "Bearer test-token"})
        assert health_response.status_code == 200
        assert health_response.json()["machineSummary"]["onlineMachines"] == 1

        machines_response = client.get("/api/remote-codex/machines", headers={"Authorization": "Bearer test-token"})
        assert machines_response.status_code == 200
        assert machines_response.json()["machines"][0]["machineId"] == "machine-z"
        assert machines_response.json()["machines"][0]["activeTransport"] == "filesystem-storage"
        assert machines_response.json()["machines"][0]["runtimeMode"] == "standalone"
        assert machines_response.json()["machines"][0]["capabilities"]["liveControl"] is True

        threads_response = client.get(
            "/api/remote-codex/machines/machine-z/threads",
            headers={"Authorization": "Bearer test-token"},
        )
        assert threads_response.status_code == 200
        assert threads_response.json()["threads"][0]["id"] == "thread-z"

        messages_response = client.get(
            "/api/remote-codex/machines/machine-z/threads/thread-z/messages",
            headers={"Authorization": "Bearer test-token"},
        )
        assert messages_response.status_code == 200
        assert [item["lineNumber"] for item in messages_response.json()["messages"]] == [1, 2]
        assert messages_response.json()["messages"][0]["images"][0]["src"] == "data:image/png;base64,abc123"

        turn_response = client.post(
            "/api/remote-codex/machines/machine-z/threads/thread-z/turns",
            headers={"Authorization": "Bearer test-token"},
            json={"prompt": "Add a task panel."},
        )
        assert turn_response.status_code == 200
        turn_payload = turn_response.json()
        assert turn_payload["task"]["status"] == "queued"
        assert turn_payload["command"]["status"] == "queued"

        commands_response = client.get(
            "/api/remote-codex/machines/machine-z/threads/thread-z/commands",
            headers={"Authorization": "Bearer test-token"},
        )
        assert commands_response.status_code == 200
        assert commands_response.json()["commands"][0]["type"] == "turn.start"

        claim_response = client.post(
            "/api/remote-codex/agent/commands/claim",
            headers={"Authorization": "Bearer test-token"},
            json={"machineId": "machine-z", "workerId": "worker-z"},
        )
        assert claim_response.status_code == 200
        claimed_command = claim_response.json()["command"]
        assert claimed_command["status"] == "running"

        result_response = client.post(
            f"/api/remote-codex/agent/commands/{claimed_command['commandId']}/result",
            headers={"Authorization": "Bearer test-token"},
            json={
                "workerId": "worker-z",
                "status": "completed",
                "result": {
                    "turnId": "turn-123",
                    "turnStatus": "inProgress",
                },
            },
        )
        assert result_response.status_code == 200
        assert result_response.json()["command"]["status"] == "completed"

        queued_followup_response = client.post(
            "/api/remote-codex/machines/machine-z/threads/thread-z/turns",
            headers={"Authorization": "Bearer test-token"},
            json={"prompt": "Queue this after the in-progress turn."},
        )
        assert queued_followup_response.status_code == 200
        queued_followup_payload = queued_followup_response.json()
        assert queued_followup_payload["task"]["status"] == "queued"
        assert queued_followup_payload["command"]["status"] == "queued"

        queued_messages_response = client.get(
            "/api/remote-codex/machines/machine-z/threads/thread-z/messages",
            headers={"Authorization": "Bearer test-token"},
        )
        assert queued_messages_response.status_code == 200
        assert [item["text"] for item in queued_messages_response.json()["messages"]] == [
            "Ship this UX",
            "Working on it.",
            "Add a task panel.",
            "Queue this after the in-progress turn.",
        ]

        task_response = client.get(
            "/api/remote-codex/machines/machine-z/threads/thread-z/tasks",
            headers={"Authorization": "Bearer test-token"},
        )
        assert task_response.status_code == 200
        tasks = task_response.json()["tasks"]
        assert len(tasks) == 2
        tasks_by_id = {task["taskId"]: task for task in tasks}
        assert tasks_by_id[queued_followup_payload["task"]["taskId"]]["status"] == "queued"
        assert tasks_by_id[queued_followup_payload["task"]["taskId"]]["currentClaim"] is None
        assert tasks_by_id[turn_payload["task"]["taskId"]]["status"] == "completed"
        assert tasks_by_id[turn_payload["task"]["taskId"]]["currentClaim"] is None

        commands_response = client.get(
            "/api/remote-codex/machines/machine-z/threads/thread-z/commands",
            headers={"Authorization": "Bearer test-token"},
        )
        assert commands_response.status_code == 200
        turn_start_commands = [command for command in commands_response.json()["commands"] if command["type"] == "turn.start"]
        assert len(turn_start_commands) >= 2
        assert turn_start_commands[0]["commandId"] == queued_followup_payload["command"]["commandId"]

        second_claim_response = client.post(
            "/api/remote-codex/agent/commands/claim",
            headers={"Authorization": "Bearer test-token"},
            json={"machineId": "machine-z", "workerId": "worker-z"},
        )
        assert second_claim_response.status_code == 200
        second_claimed_command = second_claim_response.json()["command"]
        assert second_claimed_command["commandId"] == queued_followup_payload["command"]["commandId"]
        assert second_claimed_command["status"] == "running"

        second_result_response = client.post(
            f"/api/remote-codex/agent/commands/{second_claimed_command['commandId']}/result",
            headers={"Authorization": "Bearer test-token"},
            json={
                "workerId": "worker-z",
                "status": "completed",
                "result": {
                    "turnId": "turn-124",
                    "turnStatus": "queued",
                },
            },
        )
        assert second_result_response.status_code == 200
        assert second_result_response.json()["command"]["status"] == "completed"

        second_sync_payload = {
            "machine": machine_payload["machine"],
            "threads": machine_payload["threads"],
            "snapshots": [
                {
                    "thread": machine_payload["snapshots"][0]["thread"],
                    "messages": [
                        machine_payload["snapshots"][0]["messages"][0],
                        machine_payload["snapshots"][0]["messages"][1],
                        {
                            "lineNumber": 3,
                            "timestamp": "2026-04-23T00:00:03+00:00",
                            "role": "user",
                            "phase": None,
                            "text": "Add a task panel.",
                            "images": [],
                        },
                        {
                            "lineNumber": 4,
                            "timestamp": "2026-04-23T00:00:04+00:00",
                            "role": "assistant",
                            "phase": "inProgress",
                            "text": "First request accepted.",
                            "images": [],
                        },
                        {
                            "lineNumber": 5,
                            "timestamp": "2026-04-23T00:00:05+00:00",
                            "role": "user",
                            "phase": None,
                            "text": "Queue this after the in-progress turn.",
                            "images": [],
                        },
                        {
                            "lineNumber": 6,
                            "timestamp": "2026-04-23T00:00:06+00:00",
                            "role": "assistant",
                            "phase": "inProgress",
                            "text": "Queued follow-up accepted.",
                            "images": [],
                        },
                    ],
                    "totalMessages": 6,
                    "lineCount": 6,
                    "fileSize": 256,
                    "syncedAt": "2026-04-23T00:00:07+00:00",
                }
            ],
        }
        second_sync_response = client.post(
            "/api/remote-codex/agent/sync",
            headers={"Authorization": "Bearer test-token"},
            json=second_sync_payload,
        )
        assert second_sync_response.status_code == 200

        synced_messages_response = client.get(
            "/api/remote-codex/machines/machine-z/threads/thread-z/messages",
            headers={"Authorization": "Bearer test-token"},
        )
        assert synced_messages_response.status_code == 200
        assert [item["text"] for item in synced_messages_response.json()["messages"]] == [
            "Ship this UX",
            "Working on it.",
            "Add a task panel.",
            "First request accepted.",
            "Queue this after the in-progress turn.",
            "Queued follow-up accepted.",
        ]
        assert all(item["lineNumber"] > 0 for item in synced_messages_response.json()["messages"])

        delete_response = client.delete(
            "/api/remote-codex/machines/machine-z/threads/thread-z",
            headers={"Authorization": "Bearer test-token"},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["command"]["type"] == "thread.delete"


def test_remote_codex_task_lifecycle_routes_cover_approval_interrupt_and_evidence(app_env) -> None:
    from app.main import app

    with TestClient(app) as client:
        create_response = client.post(
            "/api/remote-codex/tasks",
            headers={"Authorization": "Bearer test-token"},
            json={
                "machine_id": "machine-q",
                "thread_id": "thread-q",
                "objective": "Verify the browser task panel can trust remote_codex state.",
                "success_criteria": {"browser": ["task row", "approval status", "interrupt status"]},
                "created_by": "browser-user",
                "origin_surface": "browser",
            },
        )
        assert create_response.status_code == 200
        task_id = create_response.json()["task"]["taskId"]

        claim_response = client.post(
            "/api/remote-codex/machines/machine-q/tasks/claim-next",
            headers={"Authorization": "Bearer test-token"},
            json={
                "actor_id": "machine-q",
                "lease_seconds": 90,
            },
        )
        assert claim_response.status_code == 200
        claimed_task = claim_response.json()["task"]
        assert claimed_task["taskId"] == task_id
        lease_token = claimed_task["currentClaim"]["leaseToken"]

        heartbeat_response = client.post(
            f"/api/remote-codex/tasks/{task_id}/heartbeat",
            headers={"Authorization": "Bearer test-token"},
            json={
                "actor_id": "machine-q",
                "lease_token": lease_token,
                "phase": "executing",
                "summary": "Starting work without evidence yet.",
                "commands_run_count": 0,
                "files_read_count": 0,
                "files_modified_count": 0,
                "tests_run_count": 0,
            },
        )
        assert heartbeat_response.status_code == 200
        assert heartbeat_response.json()["task"]["status"] == "claimed"

        evidence_response = client.post(
            f"/api/remote-codex/tasks/{task_id}/evidence",
            headers={"Authorization": "Bearer test-token"},
            json={
                "actor_id": "machine-q",
                "kind": "command_execution",
                "summary": "Ran a real command for this task.",
                "payload": {"commandId": "command-q"},
            },
        )
        assert evidence_response.status_code == 200
        assert evidence_response.json()["task"]["status"] == "executing"

        agent_heartbeat_response = client.post(
            f"/api/remote-codex/agent/tasks/{task_id}/heartbeat",
            headers={"Authorization": "Bearer test-token"},
            json={
                "actorId": "machine-q",
                "phase": "executing",
                "summary": "Starting work without evidence yet.",
                "commandsRunCount": 1,
                "filesReadCount": 0,
                "filesModifiedCount": 0,
                "testsRunCount": 0,
            },
        )
        assert agent_heartbeat_response.status_code == 200
        assert agent_heartbeat_response.json()["task"]["status"] == "executing"

        agent_evidence_response = client.post(
            f"/api/remote-codex/agent/tasks/{task_id}/evidence",
            headers={"Authorization": "Bearer test-token"},
            json={
                "actorId": "machine-q",
                "kind": "command_execution",
                "summary": "Ran a real command for this task.",
                "payload": {"commandId": "command-q"},
            },
        )
        assert agent_evidence_response.status_code == 200
        assert agent_evidence_response.json()["task"]["status"] == "executing"

        approval_request = client.post(
            f"/api/remote-codex/tasks/{task_id}/approval",
            headers={"Authorization": "Bearer test-token"},
            json={
                "actor_id": "machine-q",
                "lease_token": lease_token,
                "reason": "Need explicit approval before touching visible UI state.",
                "note": "This changes the browser-facing workflow.",
            },
        )
        assert approval_request.status_code == 200
        assert approval_request.json()["task"]["status"] == "blocked_approval"

        approval_resolve = client.post(
            f"/api/remote-codex/tasks/{task_id}/approval/resolve",
            headers={"Authorization": "Bearer test-token"},
            json={
                "status": "approved",
                "resolvedBy": "Semirain",
                "note": "Proceed.",
            },
        )
        assert approval_resolve.status_code == 200
        assert approval_resolve.json()["task"]["status"] == "claimed"
        assert approval_resolve.json()["task"]["latestApproval"]["status"] == "approved"

        interrupt_response = client.post(
            f"/api/remote-codex/tasks/{task_id}/interrupt",
            headers={"Authorization": "Bearer test-token"},
            json={
                "actor_id": "machine-q",
                "lease_token": lease_token,
                "note": "Stop this run for a handoff.",
            },
        )
        assert interrupt_response.status_code == 200
        assert interrupt_response.json()["task"]["status"] == "interrupted"


def test_remote_codex_keeps_only_recent_message_window_in_bridge_storage(app_env) -> None:
    from app.main import app

    messages = [
        {
            "lineNumber": index,
            "timestamp": f"2026-04-23T00:00:{index % 60:02d}+00:00",
            "role": "user" if index % 2 else "assistant",
            "phase": None if index % 2 else "completed",
            "text": f"message {index}",
            "images": [],
        }
        for index in range(1, 261)
    ]

    sync_payload = {
        "machine": {
            "machineId": "machine-window",
            "displayName": "Machine Window",
            "source": "agent",
            "activeTransport": "standalone-app-server",
            "runtimeMode": "standalone-app-server",
            "runtimeAvailable": True,
            "capabilities": {"liveControl": True},
            "lastSeenAt": "2026-04-23T00:00:00+00:00",
            "lastSyncAt": "2026-04-23T00:00:00+00:00",
        },
        "threads": [
            {
                "id": "thread-window",
                "title": "Recent window only",
                "cwd": "C:/repo",
                "rolloutPath": "C:/repo/.codex/rollout.jsonl",
                "updatedAtMs": 1700000000000,
                "createdAtMs": 1699999999000,
                "source": "app-server",
                "modelProvider": "openai",
                "model": "gpt-5.4",
                "reasoningEffort": "medium",
                "cliVersion": "1.0.0",
                "firstUserMessage": "message 1",
                "status": {"type": "notLoaded"},
            }
        ],
        "snapshots": [
            {
                "thread": {
                    "id": "thread-window",
                    "title": "Recent window only",
                    "cwd": "C:/repo",
                    "rolloutPath": "C:/repo/.codex/rollout.jsonl",
                    "updatedAtMs": 1700000000000,
                    "createdAtMs": 1699999999000,
                    "source": "app-server",
                    "modelProvider": "openai",
                    "model": "gpt-5.4",
                    "reasoningEffort": "medium",
                    "cliVersion": "1.0.0",
                    "firstUserMessage": "message 1",
                    "status": {"type": "notLoaded"},
                },
                "messages": messages,
                "totalMessages": 260,
                "lineCount": 260,
                "fileSize": 4096,
                "syncedAt": "2026-04-23T00:00:00+00:00",
            }
        ],
    }

    with TestClient(app) as client:
        sync_response = client.post(
            "/api/remote-codex/agent/sync",
            headers={"Authorization": "Bearer test-token"},
            json=sync_payload,
        )
        assert sync_response.status_code == 200

        messages_response = client.get(
            "/api/remote-codex/machines/machine-window/threads/thread-window/messages?limit=300",
            headers={"Authorization": "Bearer test-token"},
        )
        assert messages_response.status_code == 200
        payload = messages_response.json()
        assert payload["totalMessages"] == 260
        assert len(payload["messages"]) == 200
        assert payload["messages"][0]["lineNumber"] == 61
        assert payload["messages"][-1]["lineNumber"] == 260


def test_remote_codex_thread_tasks_cleanup_superseded_executing_work(app_env) -> None:
    from app.behaviors.remote_codex.service import RemoteCodexBehaviorService
    from app.schemas import RemoteTaskClaimNextRequest

    service = RemoteCodexBehaviorService()
    now = datetime.now(timezone.utc).isoformat()

    machine_payload = {
        "machineId": "machine-stale",
        "displayName": "Machine Stale",
        "source": "agent",
        "activeTransport": "standalone-app-server",
        "runtimeMode": "standalone-app-server",
        "runtimeAvailable": True,
        "capabilities": {"liveControl": True},
        "lastSeenAt": now,
        "lastSyncAt": now,
    }
    thread_payload = {
        "id": "thread-stale",
        "title": "Stale queue cleanup",
        "cwd": "C:/repo",
        "rolloutPath": "C:/repo/.codex/rollout.jsonl",
        "updatedAtMs": 1700000000000,
        "createdAtMs": 1699999999000,
        "source": "app-server",
        "modelProvider": "openai",
        "model": "gpt-5.4",
        "reasoningEffort": "medium",
        "cliVersion": "1.0.0",
        "firstUserMessage": "first",
        "status": {"type": "active"},
    }

    service.apply_agent_sync(machine=machine_payload, threads=[thread_payload], snapshots=[])

    first = service.enqueue_turn(
        machine_id="machine-stale",
        thread_id="thread-stale",
        prompt="old task",
        requested_by={"authMethod": "token", "email": "user@example.com"},
    )
    first_task_id = first["task"]["taskId"]
    service.claim_next_machine_task(
        "machine-stale",
        RemoteTaskClaimNextRequest(actor_id="machine-stale", lease_seconds=90),
    )
    service.agent_add_evidence(
        first_task_id,
        actor_id="machine-stale",
        kind="command_execution",
        summary="Submitted first command.",
        payload={"kind": "command_execution"},
    )
    service.agent_heartbeat_task(
        first_task_id,
        actor_id="machine-stale",
        phase="executing",
        summary="First task is executing.",
        commands_run_count=1,
    )
    first_command = service.claim_next_command(machine_id="machine-stale", worker_id="worker-a")["command"]
    service.record_command_result(
        first_command["commandId"],
        worker_id="worker-a",
        status="completed",
        result={"accepted": True, "turnId": "turn-1", "turnStatus": "inProgress"},
    )

    second = service.enqueue_turn(
        machine_id="machine-stale",
        thread_id="thread-stale",
        prompt="new task",
        requested_by={"authMethod": "token", "email": "user@example.com"},
    )
    second_task_id = second["task"]["taskId"]
    service.claim_next_machine_task(
        "machine-stale",
        RemoteTaskClaimNextRequest(actor_id="machine-stale", lease_seconds=90),
    )
    service.agent_add_evidence(
        second_task_id,
        actor_id="machine-stale",
        kind="command_execution",
        summary="Submitted second command.",
        payload={"kind": "command_execution"},
    )
    service.agent_heartbeat_task(
        second_task_id,
        actor_id="machine-stale",
        phase="executing",
        summary="Second task is executing.",
        commands_run_count=1,
    )
    second_command = service.claim_next_command(machine_id="machine-stale", worker_id="worker-b")["command"]
    service.record_command_result(
        second_command["commandId"],
        worker_id="worker-b",
        status="completed",
        result={"accepted": True, "turnId": "turn-2", "turnStatus": "inProgress"},
    )

    refreshed_thread = dict(thread_payload)
    refreshed_thread["updatedAtMs"] = 1700000005000
    refreshed_thread["status"] = {"type": "active"}
    service.apply_agent_sync(machine=machine_payload, threads=[refreshed_thread], snapshots=[])

    tasks_payload = service.list_thread_tasks("machine-stale", "thread-stale")
    tasks_by_id = {task["taskId"]: task for task in tasks_payload["tasks"]}

    assert tasks_by_id[first_task_id]["status"] == "completed"
    assert tasks_by_id[second_task_id]["status"] in {"claimed", "executing"}
