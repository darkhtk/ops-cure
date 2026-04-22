from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from conftest import FakeThreadManager, NAS_BRIDGE_ROOT, OPS_CURE_ROOT


class FakeChatParticipantRuntime:
    def __init__(self) -> None:
        self.calls = []

    def generate_reply(self, context):
        from pc_launcher.connectors.chat_participant.runtime import ReplyResult

        self.calls.append(context)
        latest = context.recent_messages[-1]["content"]
        return ReplyResult(content=f"{context.actor_name} reply: {latest}")


class SlowFakeChatParticipantRuntime:
    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.calls = []

    def generate_reply(self, context):
        from pc_launcher.connectors.chat_participant.runtime import ReplyResult

        self.calls.append(context)
        time.sleep(self.delay_seconds)
        latest = context.recent_messages[-1]["content"]
        return ReplyResult(content=f"{context.actor_name} final: {latest}")


class ServiceBackedChatBridge:
    def __init__(self, *, chat_service, space_service, actor_service) -> None:
        self.chat_service = chat_service
        self.space_service = space_service
        self.actor_service = actor_service

    def get_space_by_thread(self, *, thread_id: str) -> dict:
        return self.space_service.get_space_by_thread(thread_id=thread_id).model_dump(mode="json")

    def get_actors_for_space(self, *, space_id: str) -> dict:
        return self.actor_service.get_actors_for_space(space_id=space_id).model_dump(mode="json")

    def register_chat_participant(self, *, thread_id: str, actor_name: str, actor_kind: str = "ai") -> dict:
        return self.chat_service.register_participant(
            thread_id=thread_id,
            actor_name=actor_name,
            actor_kind=actor_kind,
        ).model_dump(mode="json")

    def heartbeat_chat_participant(self, *, thread_id: str, actor_name: str) -> dict:
        return self.chat_service.heartbeat_participant(
            thread_id=thread_id,
            actor_name=actor_name,
        ).model_dump(mode="json")

    def get_chat_delta(
        self,
        *,
        thread_id: str,
        actor_name: str,
        after_message_id: str | None = None,
        limit: int = 20,
        mark_read: bool = False,
    ) -> dict:
        return self.chat_service.get_thread_delta(
            thread_id=thread_id,
            actor_name=actor_name,
            after_message_id=after_message_id,
            limit=limit,
            mark_read=mark_read,
        ).model_dump(mode="json")

    def submit_chat_message(
        self,
        *,
        thread_id: str,
        actor_name: str,
        content: str,
        actor_kind: str = "ai",
    ) -> dict:
        return self.chat_service.submit_participant_message(
            thread_id=thread_id,
            actor_name=actor_name,
            content=content,
            actor_kind=actor_kind,
        ).model_dump(mode="json")


def bootstrap_chat(tmp_path, monkeypatch):
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
    import app.db as db
    import app.behaviors.chat.kernel_binding as chat_kernel_binding
    import app.behaviors.chat.service as chat_service_module
    import app.kernel.actors as actors_module
    import app.kernel.events as events_module
    import app.kernel.spaces as spaces_module

    config.get_settings.cache_clear()
    db.init_db()

    thread_manager = FakeThreadManager()
    chat_service = chat_service_module.ChatBehaviorService(thread_manager=thread_manager)
    kernel_binding = chat_kernel_binding.build_chat_kernel_binding()
    space_service = spaces_module.SpaceService(providers=[kernel_binding.space_provider])
    actor_service = actors_module.ActorService(providers=[kernel_binding.actor_provider])
    event_service = events_module.EventService(providers=[kernel_binding.event_provider])

    return chat_service, space_service, actor_service, event_service, thread_manager


def create_thread(chat_service, *, title: str, topic: str) -> str:
    created = asyncio.run(
        chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title=title,
            topic=topic,
            created_by="operator",
        ),
    )
    return created.discord_thread_id


def build_connector(*, bridge, runtime, actor_name: str, allow_unprompted: bool = True, progress_notice_delay_seconds: float = 3.0):
    from pc_launcher.connectors.chat_participant import (
        ChatParticipantConfig,
        ChatParticipantConnector,
        InMemoryChatParticipantStateStore,
    )

    return ChatParticipantConnector(
        bridge=bridge,
        runtime=runtime,
        state_store=InMemoryChatParticipantStateStore(),
        config=ChatParticipantConfig(
            actor_name=actor_name,
            machine_label=f"{actor_name}-pc",
            allow_unprompted=allow_unprompted,
            progress_notice_delay_seconds=progress_notice_delay_seconds,
        ),
    )


def test_chat_participant_connector_replies_once_and_shows_up_in_generic_views(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, event_service, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: connector smoke", topic="connector test")
    chat_service.record_message(thread_id=thread_id, actor_name="operator", content="@codex-b hello, please share your status.")

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    runtime = FakeChatParticipantRuntime()
    connector = build_connector(bridge=bridge, runtime=runtime, actor_name="codex-b")

    first = connector.sync_once(thread_id=thread_id)
    assert first.status == "replied"
    assert len(runtime.calls) == 1

    chat_space = space_service.get_space_by_thread(thread_id=thread_id)
    chat_events = event_service.get_events_for_space(space_id=chat_space.id, limit=10)
    assert [event.actor_name for event in chat_events.events] == ["operator", "codex-b"]

    second = connector.sync_once(thread_id=thread_id)
    assert second.reason == "self_only_messages"

    chat_service.record_message(thread_id=thread_id, actor_name="operator", content="This is a monologue that does not address any participant.")
    third = connector.sync_once(thread_id=thread_id)
    assert third.reason == "reply_submitted"
    assert len(runtime.calls) == 2


def test_chat_participant_connector_can_still_run_targeted_only(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, _, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: targeted only", topic="connector test")
    chat_service.record_message(thread_id=thread_id, actor_name="operator", content="This is a monologue that does not address any participant.")

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    connector = build_connector(bridge=bridge, runtime=FakeChatParticipantRuntime(), actor_name="codex-b", allow_unprompted=False)

    result = connector.sync_once(thread_id=thread_id)
    assert result.reason == "not_addressed_to_actor"


def test_chat_participant_connector_posts_progress_notice_for_slow_runtime(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, event_service, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: slow runtime", topic="progress notice")
    chat_service.record_message(thread_id=thread_id, actor_name="operator", content="Please investigate the issue and report back.")

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    connector = build_connector(
        bridge=bridge,
        runtime=SlowFakeChatParticipantRuntime(delay_seconds=0.05),
        actor_name="codex-b",
        progress_notice_delay_seconds=0.01,
    )

    result = connector.sync_once(thread_id=thread_id)
    assert result.status == "replied"
    assert result.progress_message_id is not None

    thread = space_service.get_space_by_thread(thread_id=thread_id)
    events = event_service.get_events_for_space(space_id=thread.id, limit=10)
    assert [event.actor_name for event in events.events] == ["operator", "codex-b", "codex-b"]


def test_chat_participant_connector_skips_control_messages(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, _, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: control", topic="control test")
    chat_service.record_message(thread_id=thread_id, actor_name="operator", content="\ub2e4 \ub300\ud654 \uba48\ucdb0\ub77c")

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    connector = build_connector(bridge=bridge, runtime=FakeChatParticipantRuntime(), actor_name="codex-b")

    result = connector.sync_once(thread_id=thread_id)
    assert result.reason == "control_message"


def test_chat_participant_connector_ignores_other_ai_progress_messages(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, _, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: ignore ai progress", topic="ai message test")
    chat_service.record_message(thread_id=thread_id, actor_name="operator", content="status check")
    chat_service.register_participant(thread_id=thread_id, actor_name="codex-desktop", actor_kind="ai")
    chat_service.submit_participant_message(
        thread_id=thread_id,
        actor_name="codex-desktop",
        actor_kind="ai",
        content=(
            "\ud655\uc778\ud588\ub2e4. \uc9c0\uae08 \ubc14\ub85c \ud655\uc778\ud558\uace0 "
            "\uc9c4\ud589 \uc911\uc774\ub2e4. \ub05d\ub098\uba74 \uc5ec\uae30 \ubcf4\uace0\ud558\uaca0\ub2e4."
        ),
    )

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    connector = build_connector(bridge=bridge, runtime=FakeChatParticipantRuntime(), actor_name="codex-homedev")

    result = connector.sync_once(thread_id=thread_id)
    assert result.reason == "ai_echo_message"


def test_chat_participant_connector_allows_non_targeted_ai_collaboration_messages(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, _, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: ai collaboration", topic="ai collaboration test")
    chat_service.submit_participant_message(
        thread_id=thread_id,
        actor_name="codex-desktop",
        actor_kind="ai",
        content="I found the likely root cause in runner.py. We should move the reconnect status into the browser state model.",
    )

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    runtime = FakeChatParticipantRuntime()
    connector = build_connector(bridge=bridge, runtime=runtime, actor_name="codex-homedev")

    result = connector.sync_once(thread_id=thread_id)
    assert result.reason == "reply_submitted"
    assert len(runtime.calls) == 1


def test_chat_participant_connector_does_not_post_progress_notice_for_ai_collaboration(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, event_service, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: ai collaboration slow", topic="ai collaboration slow test")
    chat_service.submit_participant_message(
        thread_id=thread_id,
        actor_name="codex-desktop",
        actor_kind="ai",
        content="I am still comparing the reconnect and backfill paths. Keep testing while I narrow it down.",
    )

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    connector = build_connector(
        bridge=bridge,
        runtime=SlowFakeChatParticipantRuntime(delay_seconds=0.05),
        actor_name="codex-homedev",
        progress_notice_delay_seconds=0.01,
    )

    result = connector.sync_once(thread_id=thread_id)
    assert result.status == "replied"
    assert result.progress_message_id is None

    thread = space_service.get_space_by_thread(thread_id=thread_id)
    events = event_service.get_events_for_space(space_id=thread.id, limit=10)
    assert [event.actor_name for event in events.events] == ["codex-desktop", "codex-homedev"]


def test_chat_participant_connector_claims_only_one_unprompted_turn(tmp_path, monkeypatch):
    chat_service, space_service, actor_service, _, _ = bootstrap_chat(tmp_path, monkeypatch)
    thread_id = create_thread(chat_service, title="codex-chat: claim", topic="claim test")
    chat_service.register_participant(thread_id=thread_id, actor_name="codex-a", actor_kind="ai")
    chat_service.register_participant(thread_id=thread_id, actor_name="codex-b", actor_kind="ai")
    chat_service.record_message(
        thread_id=thread_id,
        actor_name="operator",
        content="\uc774\uac74 \ud3c9\ubb38 \uc694\uccad\uc774\ub2e4. \ud55c \uba85\ub9cc \ub2f5\ud574\ub77c.",
    )

    bridge = ServiceBackedChatBridge(chat_service=chat_service, space_service=space_service, actor_service=actor_service)
    connector_a = build_connector(bridge=bridge, runtime=FakeChatParticipantRuntime(), actor_name="codex-a")
    connector_b = build_connector(bridge=bridge, runtime=FakeChatParticipantRuntime(), actor_name="codex-b")

    space = bridge.get_space_by_thread(thread_id=thread_id)
    actors = bridge.get_actors_for_space(space_id=space["id"])
    delta = bridge.get_chat_delta(thread_id=thread_id, actor_name="codex-a", after_message_id=None, limit=20, mark_read=False)
    target_message = delta["messages"][-1]

    gate_a = connector_a._reply_gate_reason(
        target_message=target_message,
        participants=delta.get("participants") or actors.get("actors") or [],
        recent_messages=delta["messages"],
    )
    gate_b = connector_b._reply_gate_reason(
        target_message=target_message,
        participants=delta.get("participants") or actors.get("actors") or [],
        recent_messages=delta["messages"],
    )

    assert sorted([gate_a, gate_b], key=lambda item: "" if item is None else item) == [None, "turn_claimed_by_other_participant"]


def test_json_state_store_persists_cursor(tmp_path):
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    from pc_launcher.connectors.chat_participant import JsonFileChatParticipantStateStore

    state_path = tmp_path / "chat-state.json"
    first = JsonFileChatParticipantStateStore(path=state_path)
    assert first.get_cursor(actor_name="codex-a", thread_id="thread-1") is None
    assert first.get_event_cursor(actor_name="codex-a", thread_id="thread-1") is None

    first.set_cursor(actor_name="codex-a", thread_id="thread-1", message_id="message-123")
    first.set_event_cursor(actor_name="codex-a", thread_id="thread-1", event_cursor="cursor-456")
    second = JsonFileChatParticipantStateStore(path=state_path)
    assert second.get_cursor(actor_name="codex-a", thread_id="thread-1") == "message-123"
    assert second.get_event_cursor(actor_name="codex-a", thread_id="thread-1") == "cursor-456"


def test_codex_cli_chat_participant_runtime_uses_codex_exec_wrapper(tmp_path):
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    from pc_launcher.connectors.chat_participant.runtime import (
        CodexCliChatParticipantRuntime,
        CodexCliRuntimeConfig,
        ReplyContext,
    )

    captured = {}

    def fake_runner(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        captured["input"] = kwargs["input"]
        output_flag_index = command.index("--output-last-message")
        output_path = Path(command[output_flag_index + 1])
        output_path.write_text("codex-b: ack from runtime", encoding="utf-8")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    runtime = CodexCliChatParticipantRuntime(
        config=CodexCliRuntimeConfig(
            executable="codex",
            cwd=str(tmp_path),
            sandbox_mode="read-only",
            extra_args=["--skip-git-repo-check"],
        ),
        command_runner=fake_runner,
    )
    context = ReplyContext(
        actor_name="codex-b",
        actor_kind="ai",
        thread_id="thread-chat-1",
        space_id="space-1",
        room_title="connector room",
        room_topic="remote codex chat",
        machine_label="pc-b",
        participants=[
            {"actor_name": "operator", "actor_kind": "human"},
            {"actor_name": "codex-b", "actor_kind": "ai"},
        ],
        recent_messages=[
            {"actor_name": "operator", "content": "@codex-b please confirm the runner."},
        ],
    )

    reply = runtime.generate_reply(context)
    assert reply.content == "codex-b: ack from runtime"
    assert captured["command"][0].endswith("codex.cmd")
    assert captured["command"][1] == "exec"


def test_current_thread_runtime_uses_app_server_client_and_current_thread():
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    from pc_launcher.connectors.chat_participant.runtime import (
        CodexCurrentThreadChatParticipantRuntime,
        CodexCurrentThreadRuntimeConfig,
        ReplyContext,
    )

    class FakeAppServerClient:
        def __init__(self) -> None:
            self.resume_calls = []
            self.start_turn_calls = []
            self.read_calls = []

        def resume_thread(self, thread_id: str) -> dict:
            self.resume_calls.append(thread_id)
            return {"thread": {"id": thread_id}}

        def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict:
            self.read_calls.append((thread_id, include_turns))
            return {"thread": {"id": thread_id, "turns": []}}

        def start_turn(self, thread_id: str, prompt: str) -> dict:
            self.start_turn_calls.append((thread_id, prompt))
            return {"turn": {"id": "turn-123", "status": "inProgress"}}

        def wait_for_turn_completion(self, *, thread_id: str, turn_id: str, timeout_seconds: float):
            return {"id": turn_id, "status": "completed", "items": []}, "current-thread reply"

        def close(self) -> None:
            return None

    client = FakeAppServerClient()
    runtime = CodexCurrentThreadChatParticipantRuntime(
        config=CodexCurrentThreadRuntimeConfig(
            executable="codex",
            runtime_args=["app-server"],
            cwd=str(OPS_CURE_ROOT),
            thread_id="codex-thread-123",
            turn_timeout_seconds=42.0,
        ),
        client=client,
    )
    context = ReplyContext(
        actor_name="codex-homedev",
        actor_kind="ai",
        thread_id="discord-thread-1",
        space_id="space-1",
        room_title="ops room",
        room_topic="cross-codex chat",
        machine_label="HOMEDEV",
        participants=[
            {"actor_name": "operator", "actor_kind": "human"},
            {"actor_name": "codex-homedev", "actor_kind": "ai"},
        ],
        recent_messages=[
            {"actor_name": "operator", "content": "@codex-homedev are you attached to the current thread?"},
        ],
    )

    reply = runtime.generate_reply(context)
    assert reply.content == "current-thread reply"
    started_thread_id, prompt = client.start_turn_calls[0]
    assert started_thread_id == "codex-thread-123"
    assert "you may inspect files, use tools, run commands" in prompt


def test_chat_service_submit_participant_message_and_notify_posts_to_discord(tmp_path, monkeypatch):
    chat_service, _, _, event_service, thread_manager = bootstrap_chat(tmp_path, monkeypatch)
    import app.behaviors.chat.kernel_binding as chat_kernel_binding
    kernel_binding = chat_kernel_binding.build_chat_kernel_binding()

    async def scenario():
        created = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: discord mirror",
            topic="mirror test",
            created_by="operator",
        )
        response = await chat_service.submit_participant_message_and_notify(
            thread_id=created.discord_thread_id,
            actor_name="codex-homedev",
            actor_kind="ai",
            content="bridge reply into Discord",
        )
        return created, response

    created, response = asyncio.run(scenario())
    assert thread_manager.messages[-1] == (created.discord_thread_id, "**codex-homedev**: bridge reply into Discord")
    chat_events = event_service.get_events_for_space(space_id=response.thread.id, limit=10)
    assert chat_events.events[-1].content == "bridge reply into Discord"


def test_chat_service_record_message_does_not_echo_human_input_to_discord(tmp_path, monkeypatch):
    chat_service, _, _, event_service, thread_manager = bootstrap_chat(tmp_path, monkeypatch)

    async def scenario():
        created = await chat_service.create_chat_thread(
            guild_id="guild-1",
            parent_channel_id="parent-1",
            title="codex-chat: human inbound",
            topic="human path",
            created_by="operator",
        )
        before_count = len(thread_manager.messages)
        summary = chat_service.record_message(
            thread_id=created.discord_thread_id,
            actor_name="operator",
            content="hello from Discord",
        )
        return created, summary, before_count, len(thread_manager.messages)

    created, summary, before_count, after_count = asyncio.run(scenario())
    assert summary.last_actor_name == "operator"
    assert after_count == before_count
    chat_events = event_service.get_events_for_space(space_id=created.id, limit=10)
    assert chat_events.events[0].content == "hello from Discord"


def test_send_message_resolve_message_reads_utf8_file(tmp_path):
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    from pc_launcher.connectors.chat_participant.send_message import resolve_message

    message_file = tmp_path / "message.txt"
    message_file.write_text("\ucf54\ub371\uc2a4 \ud55c\uae00 \ud655\uc778", encoding="utf-8")

    resolved = resolve_message(inline_message=None, message_file=str(message_file), read_stdin=False)
    assert resolved == "\ucf54\ub371\uc2a4 \ud55c\uae00 \ud655\uc778"
