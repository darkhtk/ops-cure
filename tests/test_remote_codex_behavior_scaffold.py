from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


class StubRemoteTaskService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_task(self, payload):
        self.calls.append(("create_task", payload))
        return {"ok": True}

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        now = datetime.now(timezone.utc).isoformat()
        return {
            "id": task_id,
            "machine_id": "machine-a",
            "thread_id": "thread-a",
            "origin_surface": "browser",
            "origin_message_id": None,
            "objective": "Smoke test remote_codex router.",
            "success_criteria": {},
            "status": "queued",
            "priority": "normal",
            "owner_actor_id": None,
            "created_by": "browser",
            "created_at": now,
            "updated_at": now,
            "current_assignment": None,
            "latest_heartbeat": None,
            "recent_evidence": [],
            "latest_approval": None,
        }


def test_remote_codex_behavior_service_delegates_to_remote_task_service(app_env) -> None:
    from app.behaviors.remote_codex.service import RemoteCodexBehaviorService

    stub = StubRemoteTaskService()
    service = RemoteCodexBehaviorService(remote_task_service=stub)

    payload = object()
    response = service.create_task(payload)

    assert response == {"ok": True}
    assert stub.calls == [("create_task", payload)]


def test_remote_codex_router_uses_behavior_prefix(app_env) -> None:
    from app.behaviors.remote_codex.api import router
    from app.behaviors.remote_codex.service import RemoteCodexBehaviorService

    service = RemoteCodexBehaviorService(remote_task_service=StubRemoteTaskService())
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
    assert response.json()["id"] == "task-1"


def test_remote_codex_api_alias_matches_live_remote_task_service(app_env) -> None:
    from app.main import app

    with TestClient(app) as client:
        create_response = client.post(
            "/api/remote-codex/tasks",
            headers={"Authorization": "Bearer test-token"},
            json={
                "machine_id": "machine-z",
                "thread_id": "thread-z",
                "objective": "Verify the remote_codex alias is live.",
                "success_criteria": {"browser": ["task appears"]},
                "created_by": "browser-user",
            },
        )

        assert create_response.status_code == 200
        created = create_response.json()
        assert created["machine_id"] == "machine-z"
        assert created["status"] == "queued"

        fetch_response = client.get(
            f"/api/remote/tasks/{created['id']}",
            headers={"Authorization": "Bearer test-token"},
        )
        assert fetch_response.status_code == 200
        fetched = fetch_response.json()

        assert fetched["id"] == created["id"]
        assert fetched["objective"] == created["objective"]
        assert fetched["machine_id"] == created["machine_id"]
