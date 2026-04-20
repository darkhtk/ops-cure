from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select


def test_worker_registry_persists_launcher_catalog(app_env):
    manifest = app_env.schemas.ProjectManifest(
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

    app_env.registry.register_projects("launcher-a", "host-a", [manifest])

    import app.worker_registry as worker_registry

    reloaded_registry = worker_registry.WorkerRegistry(90)
    project = reloaded_registry.get_project("GenericProfile")
    assert project is not None
    assert project.profile_name == "GenericProfile"
    assert project.resolved_default_target_name == "GenericTarget"
    assert reloaded_registry.active_launcher_count() == 1
    assert reloaded_registry.tracked_project_count() == 1


def test_worker_registry_prunes_stale_launchers(app_env):
    manifest = app_env.schemas.ProjectManifest(
        profile_name="StaleProfile",
        default_target_name="StaleTarget",
        default_workdir=r"C:\Projects\StaleTarget",
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
    app_env.registry.register_projects("launcher-stale", "host-stale", [manifest])

    from app.models import LauncherRecordModel

    with app_env.db.session_scope() as db:
        row = db.scalar(
            select(LauncherRecordModel).where(LauncherRecordModel.launcher_id == "launcher-stale"),
        )
        assert row is not None
        row.last_seen_at = row.last_seen_at - timedelta(minutes=10)

    app_env.registry.prune_stale_launchers()

    assert app_env.registry.active_launcher_count() == 0
    assert app_env.registry.get_project("StaleProfile") is None
