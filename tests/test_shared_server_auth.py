from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def test_require_bridge_caller_exposes_generic_service_context(app_env) -> None:
    from app.auth import BridgeCaller, require_bridge_caller

    app = FastAPI()

    @app.get("/protected")
    def protected(caller: BridgeCaller = Depends(require_bridge_caller)) -> dict[str, object]:
        return caller.model_dump(mode="json")

    with TestClient(app) as client:
        response = client.get(
            "/protected",
            headers={
                "Authorization": "Bearer test-token",
                "X-Bridge-Client-Id": "browser-site",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "auth_method": "bridge-shared-bearer",
        "subject": "shared-bridge-token",
        "permissions": [
            "bridge:read",
            "bridge:write",
            "bridge:stream",
            "bridge:control",
        ],
        "asserted_client_id": "browser-site",
    }


def test_require_bridge_permissions_can_deny_missing_permission(app_env) -> None:
    from app.auth import BridgeCaller, require_bridge_permissions

    app = FastAPI()

    @app.get("/protected")
    def protected(
        caller: BridgeCaller = Depends(require_bridge_permissions("bridge:site-user")),
    ) -> dict[str, object]:
        return caller.model_dump(mode="json")

    with TestClient(app) as client:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing bridge permission: bridge:site-user"


def test_remote_codex_requested_by_uses_authenticated_service_context(app_env) -> None:
    from app.behaviors.remote_codex.api import router

    class StubRemoteCodexService:
        def __init__(self) -> None:
            self.requested_by: dict[str, object] | None = None

        def enqueue_turn(
            self,
            *,
            machine_id: str,
            thread_id: str,
            prompt: str,
            requested_by: dict[str, object],
        ) -> dict[str, object]:
            self.requested_by = requested_by
            return {
                "ok": True,
                "machineId": machine_id,
                "threadId": thread_id,
                "prompt": prompt,
            }

    service = StubRemoteCodexService()
    app = FastAPI()
    app.state.services = SimpleNamespace(remote_codex_service=service)
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post(
            "/api/remote-codex/machines/machine-a/threads/thread-a/turns",
            headers={
                "Authorization": "Bearer test-token",
                "X-Bridge-Client-Id": "browser-site",
                "X-Remote-Codex-User-Email": "attacker@example.com",
                "X-Remote-Codex-User-Name": "Attacker",
            },
            json={"prompt": "Do the thing."},
        )

    assert response.status_code == 200
    assert service.requested_by == {
        "authMethod": "bridge-shared-bearer",
        "subject": "shared-bridge-token",
        "assertedClientId": "browser-site",
    }


def test_remote_codex_route_permissions_follow_shared_auth_helpers(app_env) -> None:
    from app.auth import BridgeCaller, require_bridge_caller
    from app.behaviors.remote_codex.api import router

    class StubRemoteCodexService:
        def __init__(self) -> None:
            self.turn_calls = 0

        def get_health(self) -> dict[str, object]:
            return {"ok": True}

        def enqueue_turn(self, **_: object) -> dict[str, object]:
            self.turn_calls += 1
            return {"ok": True}

    service = StubRemoteCodexService()
    app = FastAPI()
    app.state.services = SimpleNamespace(remote_codex_service=service)
    app.dependency_overrides[require_bridge_caller] = lambda: BridgeCaller(
        permissions=("bridge:read",),
        asserted_client_id="browser-site",
    )
    app.include_router(router)

    with TestClient(app) as client:
        health_response = client.get("/api/remote-codex/health")
        turn_response = client.post(
            "/api/remote-codex/machines/machine-a/threads/thread-a/turns",
            json={"prompt": "Do the thing."},
        )

    assert health_response.status_code == 200
    assert turn_response.status_code == 403
    assert turn_response.json()["detail"] == "Missing bridge permission: bridge:control"
    assert service.turn_calls == 0
