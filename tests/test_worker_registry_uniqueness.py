from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _make_manifest(app_env):
    return app_env.schemas.ProjectManifest(
        profile_name="GenericProfile",
        default_target_name="GenericTarget",
        default_workdir=r"C:\Projects\GenericTarget",
        guild_id="guild-1",
        parent_channel_id="parent-1",
        allowed_user_ids=["user-1"],
        agents=[
            app_env.schemas.AgentManifest(
                name="coder",
                cli="claude",
                role="coding",
                prompt_file="prompts/coder.md",
                default=True,
            ),
        ],
    )


def test_register_with_same_hostname_is_a_quiet_idempotent_refresh(app_env, caplog):
    from app.worker_registry import WorkerRegistry

    registry = WorkerRegistry(stale_after_seconds=60)
    manifest = _make_manifest(app_env)

    with caplog.at_level("WARNING", logger="app.worker_registry"):
        registry.register_projects("launcher-A", "homedev", [manifest])
        registry.register_projects("launcher-A", "homedev", [manifest])

    collision_warnings = [
        record for record in caplog.records if "launcher_id collision" in record.getMessage()
    ]
    assert collision_warnings == []


def test_register_with_different_hostname_warns_about_collision(app_env, caplog):
    from app.worker_registry import WorkerRegistry

    registry = WorkerRegistry(stale_after_seconds=60)
    manifest = _make_manifest(app_env)
    registry.register_projects("launcher-A", "homedev", [manifest])

    with caplog.at_level("WARNING", logger="app.worker_registry"):
        registry.register_projects("launcher-A", "laptop", [manifest])

    collision_warnings = [
        record for record in caplog.records if "launcher_id collision" in record.getMessage()
    ]
    assert collision_warnings, "expected a collision warning when same launcher_id arrives from a different host"
    assert "homedev" in collision_warnings[-1].getMessage()
    assert "laptop" in collision_warnings[-1].getMessage()


def test_register_after_stale_window_is_a_quiet_takeover(app_env, monkeypatch, caplog):
    from app.worker_registry import WorkerRegistry
    from app import worker_registry as worker_registry_module

    registry = WorkerRegistry(stale_after_seconds=60)
    manifest = _make_manifest(app_env)
    registry.register_projects("launcher-A", "homedev", [manifest])

    # Simulate the stale window passing by advancing what utcnow() returns
    # for the second register call.
    real_utcnow = worker_registry_module.utcnow
    monkeypatch.setattr(
        worker_registry_module,
        "utcnow",
        lambda: real_utcnow() + timedelta(seconds=120),
    )

    with caplog.at_level("WARNING", logger="app.worker_registry"):
        registry.register_projects("launcher-A", "laptop", [manifest])

    collision_warnings = [
        record for record in caplog.records if "launcher_id collision" in record.getMessage()
    ]
    assert collision_warnings == [], "stale takeover should not emit a collision warning"


def test_is_launcher_id_active_elsewhere_returns_false_when_same_host(app_env):
    from app.worker_registry import WorkerRegistry

    registry = WorkerRegistry(stale_after_seconds=60)
    registry.register_projects("launcher-A", "homedev", [_make_manifest(app_env)])

    assert registry.is_launcher_id_active_elsewhere(
        launcher_id="launcher-A",
        hostname="homedev",
    ) is False


def test_is_launcher_id_active_elsewhere_detects_live_collision(app_env):
    from app.worker_registry import WorkerRegistry

    registry = WorkerRegistry(stale_after_seconds=60)
    registry.register_projects("launcher-A", "homedev", [_make_manifest(app_env)])

    assert registry.is_launcher_id_active_elsewhere(
        launcher_id="launcher-A",
        hostname="laptop",
    ) is True


def test_is_launcher_id_active_elsewhere_returns_false_after_stale(app_env, monkeypatch):
    from app.worker_registry import WorkerRegistry
    from app import worker_registry as worker_registry_module

    registry = WorkerRegistry(stale_after_seconds=60)
    registry.register_projects("launcher-A", "homedev", [_make_manifest(app_env)])

    real_utcnow = worker_registry_module.utcnow
    monkeypatch.setattr(
        worker_registry_module,
        "utcnow",
        lambda: real_utcnow() + timedelta(seconds=120),
    )

    assert registry.is_launcher_id_active_elsewhere(
        launcher_id="launcher-A",
        hostname="laptop",
    ) is False


def test_is_launcher_id_active_elsewhere_returns_false_for_unknown_id(app_env):
    from app.worker_registry import WorkerRegistry

    registry = WorkerRegistry(stale_after_seconds=60)

    assert registry.is_launcher_id_active_elsewhere(
        launcher_id="never-registered",
        hostname="anywhere",
    ) is False
