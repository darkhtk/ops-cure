from __future__ import annotations

import asyncio
import sys

from conftest import FakeThreadManager, NAS_BRIDGE_ROOT, OPS_CURE_ROOT


class FakeChatParticipantRuntime:
    def __init__(self) -> None:
        self.calls = []

    def generate_reply(self, context):
        from pc_launcher.connectors.chat_participant.runtime import ReplyResult

        self.calls.append(context)
        latest = context.recent_messages[-1]["content"]
        return ReplyResult(content=f"{context.actor_name} reply: {latest}")


class ServiceBackedChatBridge:
    def __init__(self, *, chat_service, space_service, actor_service) -> None:
        self.chat_service = chat_service
        self.space_service = space_service
        self.actor_service = actor_service

    def get_space_by_thread(self, *, thread_id: str) -> dict:
        summary = self.space_service.get_space_by_thread(thread_id=thread_id)
        assert summary is not None
        return summary.model_dump(mode="json")

    def get_actors_for_space(self, *, space_id: str) -> dict:
        response = self.actor_service.get_actors_for_space(space_id=space_id)
        assert response is not None
        return response.model_dump(mode="json")

    def register_chat_participant(self, *, thread_id: str, actor_name: str, actor_kind: str = "ai") -> dict:
        summary = self.chat_service.register_participant(
            thread_id=thread_id,
            actor_name=actor_name,
            actor_kind=actor_kind,
        )
        assert summary is not None
        return summary.model_dump(mode="json")

    def heartbeat_chat_participant(self, *, thread_id: str, actor_name: str) -> dict:
        summary = self.chat_service.heartbeat_participant(
            thread_id=thread_id,
            actor_name=actor_name,
        )
        assert summary is not None
        return summary.model_dump(mode="json")

    def get_chat_delta(
        self,
        *,
        thread_id: str,
        actor_name: str,
        after_message_id: str | None = None,
        limit: int = 20,
        mark_read: bool = False,
    ) -> dict:
        response = self.chat_service.get_thread_delta(
            thread_id=thread_id,
            actor_name=actor_name,
            after_message_id=after_message_id,
            limit=limit,
            mark_read=mark_read,
        )
        assert response is not None
        return response.model_dump(mode="json")

    def submit_chat_message(
        self,
        *,
        thread_id: str,
        actor_name: str,
        content: str,
        actor_kind: str = "ai",
    ) -> dict:
        response = self.chat_service.submit_participant_message(
            thread_id=thread_id,
            actor_name=actor_name,
            content=content,
            actor_kind=actor_kind,
        )
        assert response is not None
        return response.model_dump(mode="json")


def test_chat_participant_connector_replies_once_and_shows_up_in_generic_views(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.db as db
    import app.behaviors.chat.service as chat_service_module
    import app.behaviors.chat.kernel_binding as chat_kernel_binding
    import app.kernel.actors as actors_module
    import app.kernel.events as events_module
    import app.kernel.spaces as spaces_module
    from pc_launcher.connectors.chat_participant import (
        ChatParticipantConfig,
        ChatParticipantConnector,
        InMemoryChatParticipantStateStore,
    )

    db.init_db()

    thread_manager = FakeThreadManager()
    chat_service = chat_service_module.ChatBehaviorService(thread_manager=thread_manager)
    kernel_binding = chat_kernel_binding.build_chat_kernel_binding()
    space_service = spaces_module.SpaceService(providers=[kernel_binding.space_provider])
    actor_service = actors_module.ActorService(providers=[kernel_binding.actor_provider])
    event_service = events_module.EventService(providers=[kernel_binding.event_provider])

    async def scenario():
        return await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: connector smoke",
            topic="connector test",
            created_by="operator",
        )

    created = asyncio.run(scenario())
    thread_id = created.discord_thread_id

    chat_service.record_message(
        thread_id=thread_id,
        actor_name="operator",
        content="@codex-b 안녕? 상태 알려줘.",
    )

    bridge = ServiceBackedChatBridge(
        chat_service=chat_service,
        space_service=space_service,
        actor_service=actor_service,
    )
    runtime = FakeChatParticipantRuntime()
    state_store = InMemoryChatParticipantStateStore()
    connector = ChatParticipantConnector(
        bridge=bridge,
        runtime=runtime,
        state_store=state_store,
        config=ChatParticipantConfig(
            actor_name="codex-b",
            machine_label="pc-b",
        ),
    )

    first = connector.sync_once(thread_id=thread_id)
    assert first.status == "replied"
    assert first.replied_message_id is not None
    assert len(runtime.calls) == 1

    chat_space = space_service.get_space_by_thread(thread_id=thread_id)
    assert chat_space is not None
    assert chat_space.domain_type == "chat"

    chat_actors = actor_service.get_actors_for_space(space_id=chat_space.id)
    assert chat_actors is not None
    assert {actor.name for actor in chat_actors.actors} == {"operator", "codex-b"}

    chat_events = event_service.get_events_for_space(space_id=chat_space.id, limit=10)
    assert chat_events is not None
    assert chat_events.events[0].actor_name == "codex-b"
    assert "reply" in chat_events.events[0].content

    second = connector.sync_once(thread_id=thread_id)
    assert second.status == "idle"
    assert len(runtime.calls) == 1

    chat_service.record_message(
        thread_id=thread_id,
        actor_name="operator",
        content="이건 특정 참여자를 부르지 않는 독백이다.",
    )
    third = connector.sync_once(thread_id=thread_id)
    assert third.status == "skipped"
    assert third.reason == "not_addressed_to_actor"
    assert len(runtime.calls) == 1
