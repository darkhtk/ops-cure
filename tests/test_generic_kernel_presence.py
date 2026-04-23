from __future__ import annotations

import sys


def test_presence_service_tracks_actor_sessions_by_generic_scope(tmp_path, monkeypatch):
    nas_bridge_root = r"C:\Users\darkh\Projects\ops-cure\nas_bridge"
    if nas_bridge_root not in sys.path:
        sys.path.insert(0, nas_bridge_root)

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.db as db
    from app.kernel.presence import ActorSessionUpsertRequest, PresenceService

    db.init_db()
    service = PresenceService()

    first = service.upsert_actor_session(
        ActorSessionUpsertRequest(
            actor_id="actor-a",
            scope_kind="space",
            scope_id="space-1",
            status="active",
            ttl_seconds=60,
        ),
    )
    second = service.upsert_actor_session(
        ActorSessionUpsertRequest(
            actor_id="actor-b",
            scope_kind="space",
            scope_id="space-1",
            status="watching",
            ttl_seconds=120,
        ),
    )
    refreshed = service.upsert_actor_session(
        ActorSessionUpsertRequest(
            session_id=first.session_id,
            actor_id="actor-a",
            scope_kind="space",
            scope_id="space-1",
            status="busy",
            ttl_seconds=180,
        ),
    )

    assert refreshed.session_id == first.session_id
    assert refreshed.status == "busy"
    assert refreshed.expires_at > first.expires_at

    scope_presence = service.list_presence(scope_kind="space", scope_id="space-1")
    assert scope_presence.scope_kind == "space"
    assert scope_presence.scope_id == "space-1"
    assert {session.actor_id for session in scope_presence.sessions} == {"actor-a", "actor-b"}
    assert all(session.scope_kind == "space" for session in scope_presence.sessions)
    assert all(session.scope_id == "space-1" for session in scope_presence.sessions)
    assert second.session_id in {session.session_id for session in scope_presence.sessions}


def test_presence_service_claims_heartbeats_and_releases_generic_resource_leases(tmp_path, monkeypatch):
    nas_bridge_root = r"C:\Users\darkh\Projects\ops-cure\nas_bridge"
    if nas_bridge_root not in sys.path:
        sys.path.insert(0, nas_bridge_root)

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.db as db
    from app.kernel.presence import (
        PresenceService,
        ResourceLeaseClaimRequest,
        ResourceLeaseHeartbeatRequest,
        ResourceLeaseReleaseRequest,
    )

    db.init_db()
    service = PresenceService()

    claimed = service.claim_resource_lease(
        ResourceLeaseClaimRequest(
            resource_kind="operation",
            resource_id="operation-1",
            holder_actor_id="actor-a",
            lease_seconds=60,
        ),
    )
    assert claimed.resource_kind == "operation"
    assert claimed.resource_id == "operation-1"
    assert claimed.holder_actor_id == "actor-a"

    current = service.get_current_lease(resource_kind="operation", resource_id="operation-1")
    assert current is not None
    assert current.lease_id == claimed.lease_id

    heartbeated = service.heartbeat_resource_lease(
        lease_id=claimed.lease_id,
        payload=ResourceLeaseHeartbeatRequest(
            holder_actor_id="actor-a",
            lease_token=claimed.lease_token,
            lease_seconds=120,
            status="active",
        ),
    )
    assert heartbeated.status == "active"
    assert heartbeated.expires_at > claimed.expires_at

    try:
        service.claim_resource_lease(
            ResourceLeaseClaimRequest(
                resource_kind="operation",
                resource_id="operation-1",
                holder_actor_id="actor-b",
                lease_seconds=30,
            ),
        )
    except ValueError as exc:
        assert "already held" in str(exc)
    else:  # pragma: no cover - safety assertion
        raise AssertionError("Expected conflicting lease claim to fail.")

    released = service.release_resource_lease(
        lease_id=claimed.lease_id,
        payload=ResourceLeaseReleaseRequest(
            holder_actor_id="actor-a",
            lease_token=claimed.lease_token,
        ),
    )
    assert released.status == "released"
    assert released.released_at is not None
    assert service.get_current_lease(resource_kind="operation", resource_id="operation-1") is None

    reclaimed = service.claim_resource_lease(
        ResourceLeaseClaimRequest(
            resource_kind="operation",
            resource_id="operation-1",
            holder_actor_id="actor-b",
            lease_seconds=45,
        ),
    )
    assert reclaimed.holder_actor_id == "actor-b"
    assert reclaimed.lease_id != claimed.lease_id


def test_presence_routes_are_registered_without_changing_behavior_catalog(tmp_path, monkeypatch):
    nas_bridge_root = r"C:\Users\darkh\Projects\ops-cure\nas_bridge"
    if nas_bridge_root not in sys.path:
        sys.path.insert(0, nas_bridge_root)

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    from app.main import app

    route_paths = {route.path for route in app.routes}
    assert "/api/presence/sessions" in route_paths
    assert "/api/presence/scopes/{scope_kind}/{scope_id}" in route_paths
    assert "/api/leases" in route_paths
    assert "/api/leases/{lease_id}/heartbeat" in route_paths
    assert "/api/leases/{lease_id}/release" in route_paths
