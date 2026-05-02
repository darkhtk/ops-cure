"""PCClaudeBrain — delegates inbox events to PC claude_executor."""
from __future__ import annotations

import os
import sys

import pytest

from conftest import NAS_BRIDGE_ROOT

os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))


class _FakeRemoteClaudeService:
    """Records enqueue_run_start calls without actually queuing."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def enqueue_run_start(self, **kwargs):
        if self._fail:
            raise RuntimeError("simulated remote_claude failure")
        self.calls.append(kwargs)
        return {"ok": True, "command": {"id": "cmd-fake"}}


def test_pc_claude_brain_dispatches_speech_question():
    from app.behaviors.agent import PCClaudeBrain
    fake = _FakeRemoteClaudeService()
    brain = PCClaudeBrain(
        remote_claude_service=fake,
        machine_id="pc-A",
        cwd="/work/repo",
        actor_handle="@bridge-agent",
    )
    actions = brain.respond(
        {"text": "what is 2+2?"},
        {
            "event_kind": "chat.speech.question",
            "viewer_actor_handle": "@bridge-agent",
            "viewer_actor_id": "actor-id-1",
            "operation": {
                "id": "op-1", "kind": "inquiry",
                "title": "math", "intent": "verify",
                "state": "open", "participants": [],
            },
            "recent_events": [],
        },
    )
    # No immediate action -- response comes via PC asynchronously.
    assert actions is None
    # But enqueue_run_start was called with the right shape.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["machine_id"] == "pc-A"
    assert call["cwd"] == "/work/repo"
    assert "what is 2+2?" in call["prompt"]
    assert "@bridge-agent" in call["prompt"]
    # operation_id propagated for downstream reply-watcher
    assert call["requested_by"]["operation_id"] == "op-1"
    assert call["requested_by"]["actor_handle"] == "@bridge-agent"


def test_pc_claude_brain_ignores_lifecycle_events():
    from app.behaviors.agent import PCClaudeBrain
    fake = _FakeRemoteClaudeService()
    brain = PCClaudeBrain(
        remote_claude_service=fake, machine_id="pc-A", cwd="/x",
    )
    result = brain.respond(
        {"text": "..."},
        {"event_kind": "chat.conversation.opened", "operation": {"id": "op"}},
    )
    assert result is None
    assert fake.calls == []  # no dispatch on lifecycle


def test_pc_claude_brain_swallows_remote_failure():
    """If remote_claude is unavailable / machine offline, brain must
    not raise into the runner -- log + return None."""
    from app.behaviors.agent import PCClaudeBrain
    fake = _FakeRemoteClaudeService(fail=True)
    brain = PCClaudeBrain(
        remote_claude_service=fake, machine_id="pc-Z", cwd="/x",
    )
    result = brain.respond(
        {"text": "?"},
        {"event_kind": "chat.speech.question", "operation": {"id": "op-1"}},
    )
    assert result is None  # no actions, didn't crash


def test_pc_claude_brain_prompt_includes_recent_events():
    from app.behaviors.agent import PCClaudeBrain
    fake = _FakeRemoteClaudeService()
    brain = PCClaudeBrain(
        remote_claude_service=fake, machine_id="pc-A", cwd="/x",
        history_limit=3,
    )
    brain.respond(
        {"text": "follow up"},
        {
            "event_kind": "chat.speech.question",
            "viewer_actor_id": "me",
            "operation": {
                "id": "op", "kind": "inquiry", "title": "T",
                "state": "open", "participants": [],
            },
            "recent_events": [
                {"kind": "chat.speech.claim", "actor_id": "alice", "payload": {"text": "earlier-1"}},
                {"kind": "chat.speech.question", "actor_id": "alice", "payload": {"text": "earlier-2"}},
                {"kind": "chat.speech.claim", "actor_id": "me", "payload": {"text": "self-reply"}},
            ],
        },
    )
    prompt = fake.calls[0]["prompt"]
    assert "earlier-1" in prompt
    assert "earlier-2" in prompt
    assert "self-reply" in prompt
    assert "follow up" in prompt  # the trigger
    # Permission-mode-aware fields like model are not in prompt; they
    # go via enqueue kwargs.
    assert fake.calls[0].get("permission_mode") == "acceptEdits"


def test_agent_service_picks_pc_claude_brain_when_configured(tmp_path, monkeypatch):
    """build_default_agent_service routes BRIDGE_AGENT_BRAIN=pc-claude
    through PCClaudeBrain construction."""
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setenv("BRIDGE_AGENT_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_AGENT_BRAIN", "pc-claude")
    monkeypatch.setenv("BRIDGE_AGENT_HANDLE", "@bridge-agent")
    monkeypatch.setenv("BRIDGE_AGENT_PC_MACHINE_ID", "pc-A")
    monkeypatch.setenv("BRIDGE_AGENT_PC_CWD", "/work/repo")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.agent.service import build_default_agent_service
    from app.behaviors.agent import PCClaudeBrain
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.behaviors.chat.conversation_service import ChatConversationService
    db.init_db()

    broker = InProcessSubscriptionBroker()
    chat = ChatConversationService(subscription_broker=broker)
    fake_remote = _FakeRemoteClaudeService()
    svc = build_default_agent_service(
        broker=broker,
        chat_service=chat,
        remote_claude_service=fake_remote,
    )
    assert svc is not None
    runner = svc._runners[0]
    assert isinstance(runner._brain, PCClaudeBrain)
    assert runner.actor_handle == "@bridge-agent"


def test_pc_claude_brain_disabled_without_machine_id(tmp_path, monkeypatch):
    """BRIDGE_AGENT_BRAIN=pc-claude without machine_id -> service None +
    warning log (not crash)."""
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setenv("BRIDGE_AGENT_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_AGENT_BRAIN", "pc-claude")
    monkeypatch.delenv("BRIDGE_AGENT_PC_MACHINE_ID", raising=False)
    monkeypatch.delenv("BRIDGE_AGENT_PC_CWD", raising=False)
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.agent.service import build_default_agent_service
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.behaviors.chat.conversation_service import ChatConversationService
    db.init_db()

    svc = build_default_agent_service(
        broker=InProcessSubscriptionBroker(),
        chat_service=ChatConversationService(),
        remote_claude_service=_FakeRemoteClaudeService(),
    )
    assert svc is None
