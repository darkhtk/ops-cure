from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client(app_env, task_service):
    from app.api.kernel_tasks import router

    app = FastAPI()
    app.state.services = SimpleNamespace(kernel_task_service=task_service)
    app.include_router(router)
    return TestClient(app)


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_enqueue_then_get_round_trip(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    create = client.post(
        "/api/kernel/tasks",
        headers=_auth(),
        json={
            "space_id": "thread-A",
            "kind": "remote_codex.command",
            "payload": {"command_id": "cmd-1"},
            "requested_by": "site",
        },
    )
    assert create.status_code == 200
    body = create.json()
    task_id = body["id"]
    assert body["status"] == "queued"
    assert body["payload"] == {"command_id": "cmd-1"}

    fetched = client.get(f"/api/kernel/tasks/{task_id}", headers=_auth())
    assert fetched.status_code == 200
    assert fetched.json()["id"] == task_id


def test_claim_next_returns_envelope_with_lease_then_complete(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    client.post(
        "/api/kernel/tasks",
        headers=_auth(),
        json={"space_id": "s", "kind": "k"},
    ).raise_for_status()

    claim = client.post(
        "/api/kernel/tasks/claim-next",
        headers=_auth(),
        json={"actor_id": "worker-A", "lease_seconds": 60},
    )
    assert claim.status_code == 200
    claim_body = claim.json()
    assert claim_body is not None
    assert claim_body["task"]["status"] == "claimed"
    assert claim_body["task"]["owner_actor_id"] == "worker-A"
    lease_token = claim_body["lease_token"]
    task_id = claim_body["task"]["id"]

    completed = client.post(
        f"/api/kernel/tasks/{task_id}/complete",
        headers=_auth(),
        json={"lease_token": lease_token, "result": {"ok": True}},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["result"] == {"ok": True}


def test_claim_next_returns_null_when_queue_empty(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    response = client.post(
        "/api/kernel/tasks/claim-next",
        headers=_auth(),
        json={"actor_id": "worker-A"},
    )
    assert response.status_code == 200
    assert response.json() is None


def test_heartbeat_with_stale_lease_returns_409(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    client.post(
        "/api/kernel/tasks",
        headers=_auth(),
        json={"space_id": "s", "kind": "k"},
    ).raise_for_status()
    claim = client.post(
        "/api/kernel/tasks/claim-next",
        headers=_auth(),
        json={"actor_id": "worker-A"},
    ).json()

    response = client.post(
        f"/api/kernel/tasks/{claim['task']['id']}/heartbeat",
        headers=_auth(),
        json={"lease_token": "wrong-token", "lease_seconds": 60},
    )
    assert response.status_code == 409


def test_fail_marks_terminal(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    client.post(
        "/api/kernel/tasks",
        headers=_auth(),
        json={"space_id": "s", "kind": "k"},
    ).raise_for_status()
    claim = client.post(
        "/api/kernel/tasks/claim-next",
        headers=_auth(),
        json={"actor_id": "worker-A"},
    ).json()

    failed = client.post(
        f"/api/kernel/tasks/{claim['task']['id']}/fail",
        headers=_auth(),
        json={"lease_token": claim["lease_token"], "error": {"kind": "boom"}},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error"] == {"kind": "boom"}


def test_cancel_unclaimed_task(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    create = client.post(
        "/api/kernel/tasks",
        headers=_auth(),
        json={"space_id": "s", "kind": "k"},
    )
    task_id = create.json()["id"]

    cancelled = client.post(
        f"/api/kernel/tasks/{task_id}/cancel",
        headers=_auth(),
        json={"reason": "user cancelled"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_list_filters_by_space_kind_status(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    for space, kind in [("s1", "k1"), ("s1", "k2"), ("s2", "k1")]:
        client.post(
            "/api/kernel/tasks",
            headers=_auth(),
            json={"space_id": space, "kind": kind},
        ).raise_for_status()

    s1 = client.get("/api/kernel/tasks", headers=_auth(), params={"space_id": "s1"})
    assert {t["kind"] for t in s1.json()["tasks"]} == {"k1", "k2"}

    s1_k1 = client.get(
        "/api/kernel/tasks",
        headers=_auth(),
        params=[("space_id", "s1"), ("kinds", "k1")],
    )
    assert [t["kind"] for t in s1_k1.json()["tasks"]] == ["k1"]


def test_endpoint_requires_auth(app_env):
    from app.kernel.tasks import KernelTaskService

    service = KernelTaskService()
    client = _make_client(app_env, service)

    response = client.get("/api/kernel/tasks")
    assert response.status_code in {401, 403}
