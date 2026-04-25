from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client(app_env, approval_service):
    from app.api.kernel_approvals import router

    app = FastAPI()
    app.state.services = SimpleNamespace(kernel_approval_service=approval_service)
    app.include_router(router)
    return TestClient(app)


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_request_then_get_round_trip(app_env):
    from app.kernel.approvals import KernelApprovalService

    service = KernelApprovalService()
    client = _make_client(app_env, service)

    create = client.post(
        "/api/kernel/approvals",
        headers=_auth(),
        json={
            "space_id": "thread-A",
            "kind": "remote_codex.exec_command",
            "payload": {"command": ["ls"], "cwd": "/tmp"},
            "requested_by": "codex",
        },
    )
    assert create.status_code == 200
    body = create.json()
    approval_id = body["id"]
    assert body["status"] == "pending"
    assert body["payload"] == {"command": ["ls"], "cwd": "/tmp"}

    fetched = client.get(
        f"/api/kernel/approvals/{approval_id}",
        headers=_auth(),
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == approval_id


def test_resolve_routes_to_status(app_env):
    from app.kernel.approvals import KernelApprovalService

    service = KernelApprovalService()
    client = _make_client(app_env, service)

    create = client.post(
        "/api/kernel/approvals",
        headers=_auth(),
        json={"space_id": "s", "kind": "k"},
    )
    approval_id = create.json()["id"]

    resolved = client.post(
        f"/api/kernel/approvals/{approval_id}/resolve",
        headers=_auth(),
        json={"resolution": "approved_for_session", "resolved_by": "darkhtk", "note": "OK"},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["status"] == "approved"
    assert payload["resolution"] == "approved_for_session"
    assert payload["resolved_by"] == "darkhtk"
    assert payload["note"] == "OK"


def test_list_pending_filters_by_space_and_kind(app_env):
    from app.kernel.approvals import KernelApprovalService

    service = KernelApprovalService()
    client = _make_client(app_env, service)

    for space_id, kind in [("s1", "k1"), ("s1", "k2"), ("s2", "k1")]:
        client.post(
            "/api/kernel/approvals",
            headers=_auth(),
            json={"space_id": space_id, "kind": kind},
        ).raise_for_status()

    s1_all = client.get(
        "/api/kernel/approvals",
        headers=_auth(),
        params={"space_id": "s1"},
    )
    assert s1_all.status_code == 200
    assert {a["kind"] for a in s1_all.json()["approvals"]} == {"k1", "k2"}

    s1_k1 = client.get(
        "/api/kernel/approvals",
        headers=_auth(),
        params=[("space_id", "s1"), ("kinds", "k1")],
    )
    assert [a["kind"] for a in s1_k1.json()["approvals"]] == ["k1"]


def test_get_returns_404_for_unknown_id(app_env):
    from app.kernel.approvals import KernelApprovalService

    service = KernelApprovalService()
    client = _make_client(app_env, service)

    response = client.get(
        "/api/kernel/approvals/does-not-exist",
        headers=_auth(),
    )
    assert response.status_code == 404


def test_endpoint_requires_auth(app_env):
    from app.kernel.approvals import KernelApprovalService

    service = KernelApprovalService()
    client = _make_client(app_env, service)

    response = client.get(
        "/api/kernel/approvals",
        params={"space_id": "s"},
    )
    assert response.status_code in {401, 403}
