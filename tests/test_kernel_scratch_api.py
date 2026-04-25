from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client(app_env, scratch_service):
    from app.api.kernel_scratch import router

    app = FastAPI()
    app.state.services = SimpleNamespace(kernel_scratch_service=scratch_service)
    app.include_router(router)
    return TestClient(app)


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_kernel_scratch_set_then_get_round_trip(app_env):
    from app.kernel.scratch import KernelScratchService

    scratch = KernelScratchService()
    client = _make_client(app_env, scratch)

    set_response = client.put(
        "/api/kernel/scratch",
        headers=_auth(),
        json={
            "actor_id": "homedev",
            "space_id": "thread-a",
            "key": "remote_codex.pending_prompt",
            "value": [{"commandId": "command-1", "text": "hello"}],
        },
    )
    assert set_response.status_code == 200
    assert set_response.json() == {"ok": True}

    get_response = client.get(
        "/api/kernel/scratch",
        headers=_auth(),
        params={
            "actor_id": "homedev",
            "space_id": "thread-a",
            "key": "remote_codex.pending_prompt",
        },
    )
    assert get_response.status_code == 200
    assert get_response.json() == {
        "found": True,
        "value": [{"commandId": "command-1", "text": "hello"}],
    }


def test_kernel_scratch_get_returns_not_found_for_missing_key(app_env):
    from app.kernel.scratch import KernelScratchService

    scratch = KernelScratchService()
    client = _make_client(app_env, scratch)

    response = client.get(
        "/api/kernel/scratch",
        headers=_auth(),
        params={"actor_id": "x", "key": "nope"},
    )
    assert response.status_code == 200
    assert response.json() == {"found": False, "value": None}


def test_kernel_scratch_delete_removes_value_and_reports_outcome(app_env):
    from app.kernel.scratch import KernelScratchService

    scratch = KernelScratchService()
    client = _make_client(app_env, scratch)

    client.put(
        "/api/kernel/scratch",
        headers=_auth(),
        json={"actor_id": "x", "key": "k", "value": "v"},
    ).raise_for_status()

    delete_response = client.request(
        "DELETE",
        "/api/kernel/scratch",
        headers=_auth(),
        json={"actor_id": "x", "key": "k"},
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True, "removed": True}

    second_delete = client.request(
        "DELETE",
        "/api/kernel/scratch",
        headers=_auth(),
        json={"actor_id": "x", "key": "k"},
    )
    assert second_delete.status_code == 200
    assert second_delete.json() == {"ok": True, "removed": False}


def test_kernel_scratch_requires_auth(app_env):
    from app.kernel.scratch import KernelScratchService

    scratch = KernelScratchService()
    client = _make_client(app_env, scratch)

    response = client.get(
        "/api/kernel/scratch",
        params={"key": "x"},
    )
    assert response.status_code in {401, 403}


def test_kernel_scratch_isolates_actor_and_space_scopes_over_http(app_env):
    from app.kernel.scratch import KernelScratchService

    scratch = KernelScratchService()
    client = _make_client(app_env, scratch)

    for actor, space, value in [
        ("a", "", "actor-a-global"),
        ("b", "", "actor-b-global"),
        ("a", "s1", "actor-a-space-s1"),
    ]:
        client.put(
            "/api/kernel/scratch",
            headers=_auth(),
            json={"actor_id": actor, "space_id": space, "key": "k", "value": value},
        ).raise_for_status()

    cases = [
        (("a", ""), "actor-a-global"),
        (("b", ""), "actor-b-global"),
        (("a", "s1"), "actor-a-space-s1"),
    ]
    for (actor, space), expected in cases:
        response = client.get(
            "/api/kernel/scratch",
            headers=_auth(),
            params={"actor_id": actor, "space_id": space, "key": "k"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["found"] is True, (actor, space, body)
        assert body["value"] == expected, (actor, space, body)
