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


def build_manifest_for_profile(schemas, *, profile_name: str, project_name: str | None = None):
    target_name = project_name or profile_name
    return schemas.ProjectManifest(
        profile_name=profile_name,
        default_target_name=target_name,
        default_workdir=fr"C:\Users\darkh\Projects\{target_name}",
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


def test_handoff_creates_canonical_ready_task_and_self_claims(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner-1",
            pid_hint=1001,
        ),
    )
    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder-1",
            pid_hint=1002,
        ),
    )

    routed_agent = __import__("asyncio").run(
        app_env.session_service.route_discord_message(
            thread_id=summary.discord_thread_id,
            discord_message_id="discord-1",
            user_id="user-1",
            content="Analyze the issue and queue one focused implementation task.",
            author_name="operator",
        ),
    )
    assert routed_agent == "planner"

    planner_job = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner-1",
        ),
    )
    assert planner_job is not None
    assert planner_job.job_type == "orchestration"

    __import__("asyncio").run(
        app_env.session_service.complete_job(
            job_id=planner_job.id,
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner-1",
            output_text=(
                "[[report]]Queued one focused implementation task.[[/report]]\n"
                "[[handoff agent=\"coder\"]]\n"
                "T-201\n"
                "Target summary: Implement the first focused fix.\n"
                "Read CURRENT_STATE.md and TASK_BOARD.md first.\n"
                "Files: src/example.py, tests/test_example.py\n"
                "Done condition: The focused fix lands with regression coverage.\n"
                "[[/handoff]]"
            ),
            thread_output_text=(
                "OPS: type=handoff | task=T-201 | from=planner | to=coder | state=ready | "
                "read=CURRENT_STATE.md,TASKS/T-201.md\n"
                "HUMAN: coder가 첫 구현 작업을 진행한다."
            ),
            lease_token=planner_job.lease_token,
            task_revision=planner_job.task_revision,
            session_epoch=planner_job.session_epoch,
            pid_hint=1001,
        ),
    )

    from app.models import HandoffModel, JobModel, TaskModel

    with app_env.db.session_scope() as db:
        task = db.scalar(
            select(TaskModel)
            .where(TaskModel.session_id == summary.id)
            .where(TaskModel.task_key == "T-201"),
        )
        assert task is not None
        assert task.state == "ready"
        assert task.role == "coding"

        handoff = db.scalar(
            select(HandoffModel)
            .where(HandoffModel.session_id == summary.id)
            .where(HandoffModel.task_id == task.id),
        )
        assert handoff is not None
        assert handoff.state == "queued"

        pending_coder_jobs = list(
            db.scalars(
                select(JobModel)
                .where(JobModel.session_id == summary.id)
                .where(JobModel.agent_name == "coder")
                .where(JobModel.status == "pending"),
            ),
        )
        assert pending_coder_jobs == []

    coder_job = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder-1",
        ),
    )
    assert coder_job is not None
    assert coder_job.job_type == "handoff"
    assert coder_job.task_id is not None
    assert coder_job.lease_token is not None
    assert coder_job.task_revision >= 1
    assert "T-201" in coder_job.input_text

    with app_env.db.session_scope() as db:
        task = db.scalar(select(TaskModel).where(TaskModel.id == coder_job.task_id))
        assert task is not None
        assert task.state == "in_progress"
        assert task.current_lease_token == coder_job.lease_token
        claimed_handoff = db.scalar(
            select(HandoffModel)
            .where(HandoffModel.task_id == task.id)
            .where(HandoffModel.state == "claimed"),
        )
        assert claimed_handoff is not None


def test_quiet_discord_preserves_human_line_without_truncation(app_env):
    long_ops = "OPS: " + " ".join(["state=busy"] * 80)
    long_human = " ".join(["operator-facing"] * 120)

    compact = app_env.session_service._quiet_discord_text(
        "\n".join(
            [
                long_ops,
                f"HUMAN: {long_human}",
                "ISSUE: triage_required",
            ],
        ),
    )

    assert "[truncated]" in compact
    assert f"HUMAN: {long_human}" in compact


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


def test_quiet_discord_keeps_human_line_untruncated(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
            pid_hint=1001,
        ),
    )

    from app.models import JobModel

    with app_env.db.session_scope() as db:
        job = JobModel(
            session_id=summary.id,
            agent_name="planner",
            job_type="message",
            user_id="user-1",
            input_text="show compact output",
        )
        db.add(job)
        db.flush()
        job_id = job.id

    claimed = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
        ),
    )
    assert claimed is not None
    assert claimed.id == job_id

    long_human = " ".join(["status"] * 120)
    __import__("asyncio").run(
        app_env.session_service.complete_job(
            job_id=job_id,
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
            output_text="[[report]]ignored[[/report]]",
            thread_output_text=(
                "OPS: type=progress | actor=planner | task=T-001 | state=progress | read=CURRENT_STATE.md\n"
                f"HUMAN: {long_human}\n"
                "DONE: task=T-001"
            ),
            pid_hint=1001,
        ),
    )

    _, posted = app_env.thread_manager.messages[-1]
    assert "..." not in posted
    assert f"HUMAN: {long_human}" in posted
    assert "[truncated]" not in posted


def test_bridge_preserves_async_event_protocol_without_agent_wrapper(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
            pid_hint=1002,
        ),
    )

    from app.models import JobModel

    with app_env.db.session_scope() as db:
        job = JobModel(
            session_id=summary.id,
            agent_name="coder",
            job_type="message",
            user_id="user-1",
            input_text="T-002 continue implementation",
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

    __import__("asyncio").run(
        app_env.session_service.complete_job(
            job_id=job_id,
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
            output_text="[[report]]coder가 T-002를 마무리했다.[[/report]]",
            thread_output_text=(
                "OPS: type=done | actor=coder | task=T-002 | state=idle | read=CURRENT_STATE.md,TASKS/T-002.md\n"
                "HUMAN: coder가 T-002를 마무리했다.\n"
                "DONE: task=T-002"
            ),
            pid_hint=1002,
        ),
    )

    _, posted = app_env.thread_manager.messages[-1]
    assert posted.startswith("OPS: type=done")
    assert "HUMAN: coder가 T-002를 마무리했다." in posted
    assert "**coder**" not in posted


def test_start_defaults_to_sample_profile_when_omitted(app_env):
    sample_manifest = build_manifest_for_profile(app_env.schemas, profile_name="sample", project_name="SampleProject")
    extra_manifest = build_manifest_for_profile(app_env.schemas, profile_name="other", project_name="OtherProject")
    app_env.registry.register_projects("launcher-1", "host-1", [sample_manifest, extra_manifest])

    summary = __import__("asyncio").run(
        app_env.session_service.create_session_from_project(
            project_name="GenWorld",
            target_project_name="GenWorld",
            preset=None,
            user_id="user-1",
            guild_id="guild-1",
            parent_channel_id="parent-1",
            workdir_override=r"C:\Users\darkh\Projects\GenWorld",
        ),
    )

    assert summary.preset == "sample"


def test_bridge_queues_handoff_from_control_payload_while_posting_thread_payload(app_env):
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

    from app.models import JobModel

    with app_env.db.session_scope() as db:
        job = JobModel(
            session_id=summary.id,
            agent_name="planner",
            job_type="orchestration",
            user_id="user-1",
            input_text="T-001 analyze and hand off implementation",
        )
        db.add(job)
        db.flush()
        job_id = job.id

    claimed = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
        ),
    )
    assert claimed is not None
    assert claimed.id == job_id

    raw_output = (
        "[[report]]QA harness setup has been handed to coder.[[/report]]\n"
        "[[handoff agent=\"coder\"]]\n"
        "T-002\n"
        "Target summary: Set up the playable QA harness.\n"
        "Read CURRENT_STATE.md and TASK_BOARD.md first.\n"
        "Files: tools/qa.py\n"
        "Done condition: Harness runs locally.\n"
        "[[/handoff]]"
    )
    thread_output = (
        "OPS: type=handoff | task=T-002 | from=planner | to=coder | state=ready | "
        "read=CURRENT_STATE.md,TASKS/T-002.md\n"
        "HUMAN: QA harness setup has been handed to coder."
    )

    __import__("asyncio").run(
        app_env.session_service.complete_job(
            job_id=job_id,
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
            output_text=raw_output,
            thread_output_text=thread_output,
            pid_hint=1001,
        ),
    )

    _, posted = app_env.thread_manager.messages[-1]
    assert posted == thread_output

    coder_job = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
        ),
    )
    assert coder_job is not None
    assert coder_job.job_type == "handoff"
    assert coder_job.input_text.startswith("T-002\n")


def test_bridge_queues_discussion_jobs_from_control_payload(app_env):
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

    from app.models import JobModel

    with app_env.db.session_scope() as db:
        db.add(
            JobModel(
                session_id=summary.id,
                agent_name="reviewer",
                job_type="message",
                user_id="user-1",
                input_text="inspect the stale state",
            ),
        )
        job = JobModel(
            session_id=summary.id,
            agent_name="planner",
            job_type="discussion",
            user_id="user-1",
            input_text="open anomaly discussion",
        )
        db.add(job)
        db.flush()
        job_id = job.id

    claimed = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
        ),
    )
    assert claimed is not None
    assert claimed.id == job_id

    raw_output = (
        "[[discuss type=\"open\" ask=\"coder\" anomaly=\"A-001\"]]\n"
        "Task state and board disagree. Inspect CURRENT_TASK.md, TASK_BOARD.md, and failed task roll-up.\n"
        "[[/discuss]]\n"
        "[[report]]planner started a short anomaly discussion.[[/report]]"
    )
    thread_output = (
        "OPS: type=discuss_open | anomaly=A-001 | actor=planner | ask=coder | "
        "read=CURRENT_STATE.md,TASKS/T-021.md\n"
        "HUMAN: planner started a short anomaly discussion."
    )

    __import__("asyncio").run(
        app_env.session_service.complete_job(
            job_id=job_id,
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
            output_text=raw_output,
            thread_output_text=thread_output,
            pid_hint=1001,
        ),
    )

    _, posted = app_env.thread_manager.messages[-1]
    assert posted == thread_output

    coder_job = __import__("asyncio").run(
        app_env.session_service.claim_next_job(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
        ),
    )

    assert coder_job is not None
    assert coder_job.job_type == "discussion"
    assert "Discussion requested by `planner` for anomaly `A-001`." in coder_job.input_text
    assert "[[discuss type=\"reply\" to=\"planner\" anomaly=\"A-001\"]]" in coder_job.input_text


def test_start_workflow_keeps_windows_paths_stable_for_nas_bridge(app_env):
    manifest = build_manifest_for_profile(app_env.schemas, profile_name="sample", project_name="SampleProject")
    workflow = app_env.session_service.start_workflow

    assert workflow._canonical_workdir_value(r"C:\Users\darkh\Projects\GenWorld") == r"C:\Users\darkh\Projects\GenWorld"
    assert workflow._normalize_path_for_comparison(r"C:\Users\darkh\Projects\GenWorld") == r"c:\users\darkh\projects\genworld"
    assert workflow._matches_manifest_default_target(manifest, "SampleProject")


def test_recovery_service_cleans_up_stalled_start_session(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    from app.models import SessionModel, SessionOperationModel

    with app_env.db.session_scope() as db:
        session_row = db.scalar(select(SessionModel).where(SessionModel.id == summary.id))
        assert session_row is not None
        session_row.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    __import__("asyncio").run(
        app_env.recovery_service.recover_session(
            session_id=summary.id,
            reason="stalled-start-test",
        ),
    )

    refreshed = __import__("asyncio").run(app_env.session_service.get_session_summary(summary.id))
    assert refreshed.status == "failed_start"
    assert refreshed.desired_status == "closed"
    assert refreshed.closed_at is not None
    assert app_env.thread_manager.cleaned_threads == [
        (summary.discord_thread_id, "Ops-Cure cleaned up a stalled startup session."),
    ]

    with app_env.db.session_scope() as db:
        operation = db.scalar(
            select(SessionOperationModel)
            .where(SessionOperationModel.session_id == summary.id)
            .where(SessionOperationModel.operation_type == "start"),
        )
        assert operation is not None
        assert operation.status in {"completed", "failed"}


def test_start_reuses_existing_target_session_even_with_different_session_title(app_env):
    first_summary = __import__("asyncio").run(
        _start_session(app_env, name="Session A", target="UlalaCheese"),
    )

    reused_summary = __import__("asyncio").run(
        app_env.session_service.create_session_from_project(
            project_name="Session B",
            target_project_name="UlalaCheese",
            preset="UlalaCheese",
            user_id="user-1",
            guild_id="guild-1",
            parent_channel_id="parent-1",
        ),
    )

    assert reused_summary.id == first_summary.id
    assert len(app_env.thread_manager.created_threads) == 1


def test_cleanup_session_thread_closes_and_cleans_thread(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    cleaned = __import__("asyncio").run(
        app_env.session_service.cleanup_session_thread(
            summary.discord_thread_id,
            "user-1",
            reason="Manual cleanup requested.",
        ),
    )

    assert cleaned.id == summary.id
    assert cleaned.status == "closed"
    assert app_env.thread_manager.cleaned_threads == [
        (summary.discord_thread_id, "Session cleanup requested"),
    ]


def test_recovery_service_closes_session_when_thread_is_missing(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    app_env.thread_manager.missing_threads.add(summary.discord_thread_id)

    __import__("asyncio").run(
        app_env.recovery_service.recover_session(
            session_id=summary.id,
            reason="missing-thread-test",
        ),
    )

    refreshed = __import__("asyncio").run(app_env.session_service.get_session_summary(summary.id))
    assert refreshed.status == "closed"
    assert refreshed.desired_status == "closed"
    assert refreshed.closed_at is not None
    assert refreshed.execution_state == "thread_missing"
