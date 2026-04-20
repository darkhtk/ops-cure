from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select


def build_manifest(schemas, *, project_name: str = "UlalaCheese"):
    return schemas.ProjectManifest(
        profile_name="UlalaCheese",
        default_target_name=project_name,
        default_workdir=r"C:\Users\darkh\Projects\UlalaCheese",
        guild_id="guild-1",
        parent_channel_id="parent-1",
        allowed_user_ids=["user-1"],
        agents=[
            schemas.AgentManifest(
                name="planner",
                cli="claude",
                role="planning",
                prompt_file="prompts/planner.md",
                default=False,
            ),
            schemas.AgentManifest(
                name="coder",
                cli="claude",
                role="coding",
                prompt_file="prompts/coder.md",
                default=True,
            ),
        ],
        finder=schemas.FinderManifest(
            roots=[r"C:\Users\darkh\Projects"],
            analyze_agent="planner",
            prompt_file="prompts/finder.md",
        ),
    )


async def _start_session(
    app_env,
    *,
    name: str = "UlalaCheese",
    target: str | None = None,
    register_launcher: bool = True,
):
    manifest = build_manifest(app_env.schemas, project_name="UlalaCheese")
    if register_launcher:
        app_env.registry.register_projects(
            "launcher-1",
            "host-1",
            [manifest],
        )
    summary = await app_env.session_service.create_session_from_project(
        project_name=name,
        target_project_name=target or name,
        preset="UlalaCheese",
        user_id="user-1",
        guild_id="guild-1",
        parent_channel_id="parent-1",
    )
    return summary


def test_start_workflow_creates_targets_and_policy(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    assert summary.status == "waiting_for_workers"
    assert summary.target_project_name == "UlalaCheese"
    assert summary.power_target is not None
    assert summary.execution_target is not None
    assert summary.policy is not None
    assert summary.active_operation is None

    with app_env.db.session_scope() as db:
        from app.models import ExecutionTargetModel, PowerTargetModel, SessionOperationModel, SessionPolicyModel

        assert db.scalar(select(PowerTargetModel.name)) is not None
        assert db.scalar(select(ExecutionTargetModel.name)) is not None
        assert db.scalar(select(SessionPolicyModel.session_id)) == summary.id
        assert db.scalar(select(SessionOperationModel.operation_type)) == "start"


def test_pause_resume_and_policy_override(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    paused = __import__("asyncio").run(
        app_env.session_service.pause_session(
            session_id=summary.id,
            requested_by="user-1",
            reason="Maintenance window",
        ),
    )
    assert paused.status == "paused"
    assert paused.desired_status == "paused"

    policy = __import__("asyncio").run(
        app_env.session_service.set_policy(
            session_id=summary.id,
            key="max_parallel_agents",
            value="3",
            updated_by="user-1",
        ),
    )
    assert policy.policy.max_parallel_agents == 3
    assert policy.policy.version >= 2

    resumed = __import__("asyncio").run(
        app_env.session_service.resume_session(
            session_id=summary.id,
            requested_by="user-1",
        ),
    )
    assert resumed.desired_status == "ready"
    assert resumed.status == "waiting_for_workers"


def test_start_reuses_existing_session_when_launcher_is_offline(app_env):
    first_summary = __import__("asyncio").run(_start_session(app_env))

    from app.models import LauncherRecordModel

    with app_env.db.session_scope() as db:
        launcher = db.scalar(select(LauncherRecordModel))
        assert launcher is not None
        launcher.status = "stale"

    reused_summary = __import__("asyncio").run(
        app_env.session_service.create_session_from_project(
            project_name="UlalaCheese",
            preset="UlalaCheese",
            user_id="user-1",
            guild_id="guild-1",
            parent_channel_id="parent-1",
        ),
    )
    assert reused_summary.id == first_summary.id
    assert reused_summary.status in {"awaiting_launcher", "waking_execution_plane"}


def test_recovery_service_handles_naive_heartbeat_timestamps(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    with app_env.db.session_scope() as db:
        from app.models import AgentModel

        agent = db.scalar(
            select(AgentModel)
            .where(AgentModel.session_id == summary.id)
            .where(AgentModel.agent_name == "planner"),
        )
        assert agent is not None
        agent.worker_id = "worker-1"
        agent.last_heartbeat_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)

    __import__("asyncio").run(
        app_env.recovery_service.recover_session(
            session_id=summary.id,
            reason="naive-heartbeat-test",
        ),
    )

    with app_env.db.session_scope() as db:
        from app.models import AgentModel

        refreshed = db.scalar(
            select(AgentModel)
            .where(AgentModel.session_id == summary.id)
            .where(AgentModel.agent_name == "planner"),
        )
        assert refreshed is not None
        assert refreshed.worker_id is None


def test_start_prefers_requested_target_over_profile_default(app_env, monkeypatch):
    manifest = build_manifest(app_env.schemas, project_name="UlalaCheese")
    app_env.registry.register_projects("launcher-1", "host-1", [manifest])

    async def fake_enqueue_project_find(**kwargs):
        del kwargs
        return app_env.schemas.ProjectFindSummaryResponse(
            id="find-1",
            preset="UlalaCheese",
            query_text="GenWorld",
            status="pending",
            requested_by="user-1",
            guild_id="guild-1",
            parent_channel_id="parent-1",
            created_at=datetime.now(timezone.utc),
        )

    async def fake_wait_for_project_find(*, find_id: str, **kwargs):
        del find_id, kwargs
        return app_env.schemas.ProjectFindSummaryResponse(
            id="find-1",
            preset="UlalaCheese",
            query_text="GenWorld",
            status="selected",
            requested_by="user-1",
            guild_id="guild-1",
            parent_channel_id="parent-1",
            selected_path=r"C:\Users\darkh\Projects\GenWorld",
            selected_name="GenWorld",
            reason="Exact folder match",
            confidence=0.99,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(app_env.session_service, "enqueue_project_find", fake_enqueue_project_find)
    monkeypatch.setattr(app_env.session_service, "wait_for_project_find", fake_wait_for_project_find)

    summary = __import__("asyncio").run(
        app_env.session_service.create_session_from_project(
            project_name="GenWorld session",
            target_project_name="GenWorld",
            preset="UlalaCheese",
            user_id="user-1",
            guild_id="guild-1",
            parent_channel_id="parent-1",
        ),
    )

    assert summary.project_name == "GenWorld session"
    assert summary.target_project_name == "GenWorld"
    assert summary.workdir == r"C:\Users\darkh\Projects\GenWorld"
    assert summary.power_target is not None
    assert summary.execution_target is not None
    assert summary.power_target.name == "UlalaCheese:default"
    assert summary.execution_target.name == "UlalaCheese:default"


def test_max_parallel_agents_policy_limits_claims(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
            pid_hint=1001,
        ),
    )
    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
            pid_hint=1002,
        ),
    )
    __import__("asyncio").run(
        app_env.session_service.set_policy(
            session_id=summary.id,
            key="max_parallel_agents",
            value="1",
            updated_by="user-1",
        ),
    )

    from app.models import JobModel

    with app_env.db.session_scope() as db:
        db.add(
            JobModel(
                session_id=summary.id,
                agent_name="planner",
                job_type="message",
                user_id="user-1",
                input_text="planner work",
            ),
        )
        db.add(
            JobModel(
                session_id=summary.id,
                agent_name="coder",
                job_type="message",
                user_id="user-1",
                input_text="coder work",
            ),
        )

    planner_job = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
        ),
    )
    assert planner_job is not None

    coder_job = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
        ),
    )
    assert coder_job is None


def test_quiet_discord_policy_false_preserves_full_output(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
            pid_hint=1002,
        ),
    )
    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
            pid_hint=1001,
        ),
    )
    __import__("asyncio").run(
        app_env.session_service.set_policy(
            session_id=summary.id,
            key="quiet_discord",
            value="false",
            updated_by="user-1",
        ),
    )

    from app.models import JobModel

    with app_env.db.session_scope() as db:
        job = JobModel(
            session_id=summary.id,
            agent_name="coder",
            job_type="message",
            user_id="user-1",
            input_text="show detailed output",
        )
        db.add(job)
        db.flush()
        job_id = job.id

    claimed = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
        ),
    )
    assert claimed is not None
    assert claimed.id == job_id

    full_output = "\n".join(f"line {index}" for index in range(1, 12))
    __import__("asyncio").run(
        app_env.session_service.complete_job(
            job_id=job_id,
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
            output_text=full_output,
            pid_hint=1002,
        ),
    )

    assert app_env.thread_manager.messages
    _, posted = app_env.thread_manager.messages[-1]
    assert "line 1" in posted
    assert "line 11" in posted
