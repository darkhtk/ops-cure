from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from conftest import FakeThreadManager, NAS_BRIDGE_ROOT


def test_generic_kernel_supports_workflow_chat_and_ops_without_agents(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.db as db
    import app.behaviors.catalog as catalog
    import app.behaviors.chat.service as chat_service_module
    import app.behaviors.ops.service as ops_service_module
    import app.behaviors.registry as registry_module
    import app.behaviors.orchestration.models as orchestration_models
    import app.behaviors.orchestration.policy as policy_service_module
    import app.behaviors.orchestration.recovery as recovery_service_module
    import app.behaviors.orchestration.service as session_service_module
    import app.behaviors.orchestration.verification as verification_service_module
    import app.kernel.actors as actors_module
    import app.kernel.drift as drift_monitor_module
    import app.kernel.event_log as transcript_service_module
    import app.kernel.events as events_module
    import app.kernel.registry as worker_registry_module
    import app.kernel.spaces as spaces_module
    import app.presenters.discord.status_cards as announcement_service_module

    db.init_db()

    settings = config.get_settings()
    registry = worker_registry_module.WorkerRegistry(settings.worker_stale_after_seconds)
    thread_manager = FakeThreadManager()
    announcement_service = announcement_service_module.AnnouncementService(thread_manager=thread_manager)
    transcript_service = transcript_service_module.TranscriptService()
    chat_service = chat_service_module.ChatBehaviorService(thread_manager=thread_manager)
    ops_service = ops_service_module.OpsBehaviorService(thread_manager=thread_manager)
    policy_service = policy_service_module.PolicyService()
    verification_service = verification_service_module.VerificationService(
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
    )
    recovery_service = recovery_service_module.RecoveryService(
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
        power_provider=object(),
        execution_provider=object(),
        worker_stale_after_seconds=settings.worker_stale_after_seconds,
        stalled_start_timeout_seconds=settings.stalled_start_timeout_seconds,
    )
    session_service = session_service_module.SessionService(
        registry=registry,
        thread_manager=thread_manager,
        transcript_service=transcript_service,
        drift_monitor=drift_monitor_module.DriftMonitor(),
    )

    context = registry_module.BehaviorContext(
        registry=registry,
        thread_manager=thread_manager,
        chat_service=chat_service,
        ops_service=ops_service,
        policy_service=policy_service,
        recovery_service=recovery_service,
        session_service=session_service,
        verification_service=verification_service,
    )
    descriptors = registry_module.default_behavior_descriptors()
    kernel_bindings = registry_module.resolve_kernel_bindings(context=context, descriptors=descriptors)
    discord_bindings = registry_module.resolve_discord_bindings(context=context, descriptors=descriptors)

    behavior_catalog = catalog.BehaviorCatalogService(
        descriptors=descriptors,
        kernel_bindings=kernel_bindings,
        discord_bindings=discord_bindings,
    )
    space_service = spaces_module.SpaceService(
        providers=[binding.space_provider for binding in kernel_bindings if binding.space_provider is not None],
    )
    actor_service = actors_module.ActorService(
        providers=[binding.actor_provider for binding in kernel_bindings if binding.actor_provider is not None],
    )
    event_service = events_module.EventService(
        providers=[binding.event_provider for binding in kernel_bindings if binding.event_provider is not None],
    )

    async def scenario() -> tuple[chat_service_module.ChatThreadCreateResponse, ops_service_module.OpsThreadCreateResponse]:
        chat_row = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: smoke",
            topic="remote codex dialogue",
            created_by="alice",
        )
        chat_service.record_message(
            thread_id=chat_row.discord_thread_id,
            actor_name="alice",
            content="hello from pc-a",
        )
        chat_service.record_message(
            thread_id=chat_row.discord_thread_id,
            actor_name="bob",
            content="reply from pc-b",
        )

        ops_row = await ops_service.create_ops_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="ops: smoke",
            summary="generic incident smoke",
            created_by="operator",
        )
        ops_service.record_message(
            thread_id=ops_row.discord_thread_id,
            actor_name="operator",
            content="issue: queue is blocked",
        )
        ops_service.record_message(
            thread_id=ops_row.discord_thread_id,
            actor_name="operator",
            content="resolve: queue recovered",
        )
        return chat_row, ops_row

    chat_row, ops_row = asyncio.run(scenario())

    with db.session_scope() as session:
        workflow_session = orchestration_models.SessionModel(
            project_name="workflow smoke",
            target_project_name="workflow smoke",
            discord_thread_id="thread-workflow",
            guild_id="guild-1",
            parent_channel_id="parent-1",
            workdir=r"C:\tmp\workflow",
            status="ready",
            desired_status="ready",
            created_by="tester",
        )
        session.add(workflow_session)
        session.flush()
        session.add(
            orchestration_models.AgentModel(
                session_id=workflow_session.id,
                agent_name="planner",
                cli_type="claude",
                role="planning",
                status="idle",
            ),
        )
        session.add(
            orchestration_models.TranscriptModel(
                session_id=workflow_session.id,
                direction="outbound",
                actor="planner",
                content="workflow event one",
            ),
        )
        workflow_id = workflow_session.id

        assert session.scalar(select(orchestration_models.SessionModel.id)) is not None

    behaviors = behavior_catalog.list_behaviors()
    behavior_map = {behavior.behavior_id: behavior for behavior in behaviors}
    assert set(behavior_map) == {"orchestration", "chat", "ops", "remote_codex"}
    for behavior_id in ("orchestration", "chat", "ops"):
        assert behavior_map[behavior_id].supports_spaces
        assert behavior_map[behavior_id].supports_actors
        assert behavior_map[behavior_id].supports_events
        assert behavior_map[behavior_id].supports_discord_commands
        assert behavior_map[behavior_id].supports_discord_messages

    assert not behavior_map["remote_codex"].supports_spaces
    assert not behavior_map["remote_codex"].supports_actors
    assert not behavior_map["remote_codex"].supports_events
    assert not behavior_map["remote_codex"].supports_discord_commands
    assert not behavior_map["remote_codex"].supports_discord_messages

    chat_space = space_service.get_space(space_id=chat_row.id)
    ops_space = space_service.get_space(space_id=ops_row.id)
    workflow_space = space_service.get_space(space_id=workflow_id)
    assert chat_space is not None and chat_space.domain_type == "chat"
    assert ops_space is not None and ops_space.domain_type == "ops"
    assert workflow_space is not None and workflow_space.domain_type == "orchestration"

    chat_actors = actor_service.get_actors_for_thread(thread_id=chat_row.discord_thread_id)
    ops_actors = actor_service.get_actors_for_thread(thread_id=ops_row.discord_thread_id)
    workflow_actors = actor_service.get_actors_for_thread(thread_id="thread-workflow")
    assert chat_actors is not None and {actor.name for actor in chat_actors.actors} == {"alice", "bob"}
    assert ops_actors is not None and [actor.name for actor in ops_actors.actors] == ["operator"]
    assert workflow_actors is not None and [actor.name for actor in workflow_actors.actors] == ["planner"]

    chat_events = event_service.get_events_for_thread(thread_id=chat_row.discord_thread_id, limit=5)
    ops_events = event_service.get_events_for_thread(thread_id=ops_row.discord_thread_id, limit=5)
    workflow_events = event_service.get_events_for_thread(thread_id="thread-workflow", limit=5)
    assert chat_events is not None and [event.actor_name for event in chat_events.events] == ["alice", "bob"]
    assert ops_events is not None and [event.kind for event in ops_events.events] == ["issue", "resolve"]
    assert workflow_events is not None and workflow_events.events[0].content == "workflow event one"

    assert ops_space.metadata["issue_count"] == 1
    assert ops_space.status == "monitoring"
    assert len(thread_manager.created_threads) == 2
