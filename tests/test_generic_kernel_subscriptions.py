from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from conftest import FakeThreadManager, NAS_BRIDGE_ROOT


def test_generic_kernel_delta_and_stream_resume_without_domain_leakage(tmp_path, monkeypatch):
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
    from app.api.events import _stream_space_events
    from app.behaviors.catalog import BehaviorCatalogService
    from app.behaviors.chat.binding import build_chat_discord_binding
    from app.behaviors.chat.kernel_binding import build_chat_kernel_binding
    from app.behaviors.chat.service import ChatBehaviorService
    from app.behaviors.ops.binding import build_ops_discord_binding
    from app.behaviors.ops.kernel_binding import build_ops_kernel_binding
    from app.behaviors.ops.service import OpsBehaviorService
    from app.behaviors.orchestration.binding import build_orchestration_discord_binding
    from app.behaviors.orchestration.kernel_binding import build_orchestration_kernel_binding
    from app.behaviors.registry import default_behavior_descriptors
    from app.behaviors.orchestration.policy import PolicyService
    from app.behaviors.orchestration.recovery import RecoveryService
    from app.behaviors.orchestration.service import SessionService
    from app.behaviors.orchestration.verification import VerificationService
    from app.capabilities.execution.windows_launcher import WindowsLauncherExecutionProvider
    from app.capabilities.power.noop import NoopPowerProvider
    from app.kernel.actors import ActorService
    from app.kernel.drift import DriftMonitor
    from app.kernel.event_log import TranscriptService
    from app.kernel.events import EventService
    from app.kernel.registry import WorkerRegistry
    from app.kernel.spaces import SpaceService
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.presenters.discord.status_cards import AnnouncementService

    db.init_db()

    settings = config.get_settings()
    registry = WorkerRegistry(settings.worker_stale_after_seconds)
    thread_manager = FakeThreadManager()
    subscription_broker = InProcessSubscriptionBroker()
    announcement_service = AnnouncementService(thread_manager=thread_manager)
    transcript_service = TranscriptService(subscription_broker=subscription_broker)
    chat_service = ChatBehaviorService(
        thread_manager=thread_manager,
        subscription_broker=subscription_broker,
    )
    ops_service = OpsBehaviorService(
        thread_manager=thread_manager,
        subscription_broker=subscription_broker,
    )
    policy_service = PolicyService()
    verification_service = VerificationService(
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
    )
    recovery_service = RecoveryService(
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
        power_provider=NoopPowerProvider(),
        execution_provider=WindowsLauncherExecutionProvider(registry),
        worker_stale_after_seconds=settings.worker_stale_after_seconds,
        stalled_start_timeout_seconds=settings.stalled_start_timeout_seconds,
    )
    session_service = SessionService(
        registry=registry,
        thread_manager=thread_manager,
        transcript_service=transcript_service,
        drift_monitor=DriftMonitor(),
    )

    chat_binding = build_chat_kernel_binding()
    ops_binding = build_ops_kernel_binding()
    orchestration_binding = build_orchestration_kernel_binding()
    kernel_bindings = [chat_binding, ops_binding, orchestration_binding]

    event_service = EventService(
        providers=[binding.event_provider for binding in kernel_bindings if binding.event_provider is not None],
    )
    space_service = SpaceService(
        providers=[binding.space_provider for binding in kernel_bindings if binding.space_provider is not None],
    )
    actor_service = ActorService(
        providers=[binding.actor_provider for binding in kernel_bindings if binding.actor_provider is not None],
    )

    descriptors = default_behavior_descriptors()
    behavior_catalog = BehaviorCatalogService(
        descriptors=descriptors,
        kernel_bindings=kernel_bindings,
        discord_bindings=[
            build_orchestration_discord_binding(
                session_service=session_service,
                verification_service=verification_service,
                registry=registry,
            ),
            build_chat_discord_binding(chat_service=chat_service, thread_manager=thread_manager),
            build_ops_discord_binding(ops_service=ops_service, thread_manager=thread_manager),
        ],
    )

    async def scenario():
        created = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: stream smoke",
            topic="stream contract",
            created_by="alice",
        )
        chat_service.record_message(
            thread_id=created.discord_thread_id,
            actor_name="alice",
            content="hello one",
        )
        chat_service.record_message(
            thread_id=created.discord_thread_id,
            actor_name="bob",
            content="hello two",
        )
        return created

    created = asyncio.run(scenario())
    space = space_service.get_space(space_id=created.id)
    assert space is not None

    behavior_ids = {item.behavior_id for item in behavior_catalog.list_behaviors()}
    assert behavior_ids == {"orchestration", "chat", "ops", "remote_codex", "remote_claude"}

    initial = event_service.get_events_for_space(space_id=created.id, limit=10)
    assert initial is not None
    assert [event.actor_name for event in initial.events] == ["alice", "bob"]
    latest_cursor = initial.next_cursor
    assert latest_cursor is not None

    first_cursor = initial.items[0].cursor
    resumed = event_service.get_events_for_space(
        space_id=created.id,
        after_cursor=first_cursor,
        limit=10,
    )
    assert resumed is not None
    assert [event.actor_name for event in resumed.events] == ["bob"]

    kinds_filtered = event_service.get_events_for_space(
        space_id=created.id,
        after_cursor=None,
        limit=10,
        kinds=["message"],
    )
    assert kinds_filtered is not None
    assert all(item.event.kind == "message" for item in kinds_filtered.items)

    services = SimpleNamespace(
        event_service=event_service,
        space_service=space_service,
        subscription_broker=subscription_broker,
    )

    async def stream_and_publish():
        generator = _stream_space_events(
            services=services,
            space_id=created.id,
            after_cursor=latest_cursor,
            limit=10,
            kinds=["message"],
            subscriber_id="pc-codex-a",
        )
        open_chunk = await anext(generator)
        assert "event: open" in open_chunk
        assert latest_cursor in open_chunk

        producer = asyncio.create_task(_publish_later(chat_service, created.discord_thread_id))
        event_chunk = await anext(generator)
        await producer
        assert "event: event" in event_chunk
        assert "hello three" in event_chunk
        await generator.aclose()

    async def _publish_later(chat_service_instance, thread_id: str):
        await asyncio.sleep(0.05)
        chat_service_instance.record_message(
            thread_id=thread_id,
            actor_name="alice",
            content="hello three",
        )

    asyncio.run(stream_and_publish())

    async def stream_without_handoff_loss():
        race_thread = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: race smoke",
            topic="handoff race",
            created_by="alice",
        )
        chat_service.record_message(
            thread_id=race_thread.discord_thread_id,
            actor_name="alice",
            content="race one",
        )
        chat_service.record_message(
            thread_id=race_thread.discord_thread_id,
            actor_name="bob",
            content="race two",
        )
        race_initial = event_service.get_events_for_space(space_id=race_thread.id, limit=10)
        assert race_initial is not None
        race_cursor = race_initial.items[0].cursor

        generator = _stream_space_events(
            services=services,
            space_id=race_thread.id,
            after_cursor=race_cursor,
            limit=10,
            kinds=["message"],
            subscriber_id="pc-codex-b",
        )
        open_chunk = await anext(generator)
        assert "event: open" in open_chunk

        chat_service.record_message(
            thread_id=race_thread.discord_thread_id,
            actor_name="alice",
            content="race handoff",
        )

        first_event_chunk = await anext(generator)
        second_event_chunk = await anext(generator)
        await generator.aclose()

        assert "race two" in first_event_chunk
        assert "race handoff" in second_event_chunk

    asyncio.run(stream_without_handoff_loss())

    async def stream_with_empty_backlog_uses_persisted_replay():
        restart_thread = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: restart smoke",
            topic="restart resume",
            created_by="alice",
        )
        chat_service.record_message(
            thread_id=restart_thread.discord_thread_id,
            actor_name="alice",
            content="restart one",
        )
        chat_service.record_message(
            thread_id=restart_thread.discord_thread_id,
            actor_name="bob",
            content="restart two",
        )
        restart_initial = event_service.get_events_for_space(space_id=restart_thread.id, limit=10)
        assert restart_initial is not None
        restart_cursor = restart_initial.items[0].cursor

        subscription_broker._backlog.clear()

        generator = _stream_space_events(
            services=services,
            space_id=restart_thread.id,
            after_cursor=restart_cursor,
            limit=10,
            kinds=["message"],
            subscriber_id="pc-codex-c",
        )
        open_chunk = await anext(generator)
        assert "event: open" in open_chunk

        replay_chunk = await anext(generator)
        await generator.aclose()
        assert "restart two" in replay_chunk

    asyncio.run(stream_with_empty_backlog_uses_persisted_replay())

    async def stream_resets_when_replay_window_exceeds_limit():
        limited_thread = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: limit smoke",
            topic="limit contract",
            created_by="alice",
        )
        chat_service.record_message(
            thread_id=limited_thread.discord_thread_id,
            actor_name="alice",
            content="limit one",
        )
        chat_service.record_message(
            thread_id=limited_thread.discord_thread_id,
            actor_name="bob",
            content="limit two",
        )
        chat_service.record_message(
            thread_id=limited_thread.discord_thread_id,
            actor_name="alice",
            content="limit three",
        )
        limited_initial = event_service.get_events_for_space(space_id=limited_thread.id, limit=10)
        assert limited_initial is not None
        limited_cursor = limited_initial.items[0].cursor

        generator = _stream_space_events(
            services=services,
            space_id=limited_thread.id,
            after_cursor=limited_cursor,
            limit=1,
            kinds=["message"],
            subscriber_id="pc-codex-d",
        )
        open_chunk = await anext(generator)
        assert "event: open" in open_chunk
        reset_chunk = await anext(generator)
        await generator.aclose()
        assert "event: reset" in reset_chunk
        assert "replay_limit_exceeded" in reset_chunk

    asyncio.run(stream_resets_when_replay_window_exceeds_limit())

    latest = event_service.get_events_for_space(
        space_id=created.id,
        after_cursor=latest_cursor,
        limit=10,
    )
    assert latest is not None
    assert [event.content for event in latest.events] == ["hello three"]

    envelope = latest.items[0]
    assert sorted(envelope.event.model_dump().keys()) == ["actor_name", "content", "created_at", "id", "kind"]

    actors = actor_service.get_actors_for_space(space_id=created.id)
    assert actors is not None
    assert {actor.name for actor in actors.actors} == {"alice", "bob"}
