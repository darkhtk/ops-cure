"""P13-1: ChatConversationService.emit_system_event."""
from __future__ import annotations

import sys
import uuid

import pytest

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv(
        "BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}",
    )
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import (
        ChatConversationService, ChatConversationNotFoundError,
    )
    from app.behaviors.chat.conversation_schemas import ConversationOpenRequest
    from app.behaviors.chat.models import ChatThreadModel
    from app.kernel.presence import PresenceService
    from app.kernel.approvals import KernelApprovalService
    from app.services.remote_task_service import RemoteTaskService
    db.init_db()
    return locals()


def _open_thread_and_op(m):
    from app.behaviors.chat.models import ChatThreadModel
    with m["db"].session_scope() as s:
        t = ChatThreadModel(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{uuid.uuid4().hex[:6]}", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        thread_id = t.discord_thread_id

    rt = m["RemoteTaskService"](
        presence_service=m["PresenceService"](),
        kernel_approval_service=m["KernelApprovalService"](),
    )
    chat = m["ChatConversationService"](remote_task_service=rt)
    chat.ensure_general(discord_thread_id=thread_id)
    summary = chat.open_conversation(
        discord_thread_id=thread_id,
        request=m["ConversationOpenRequest"](
            kind="task", title="t", objective="do",
            opener_actor="alice",
        ),
    )
    return chat, summary


def test_emit_system_event_inserts_chat_message_and_v2_event(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    chat, summary = _open_thread_and_op(m)
    out = chat.emit_system_event(
        conversation_id=summary.id,
        kind="chat.system.nudge",
        addressed_to_handle="@curator",
        payload={"reason": "idle 30s; channel=expected_response"},
    )
    assert out["v1_message_id"]
    assert out["v2_event_id"]


def test_emit_system_event_unknown_conversation_raises(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    chat, _summary = _open_thread_and_op(m)
    with pytest.raises(m["ChatConversationNotFoundError"]):
        chat.emit_system_event(
            conversation_id="nope-not-here",
            kind="chat.system.nudge",
        )


def test_emit_system_event_replies_to_link(tmp_path, monkeypatch):
    """When replies_to_v2_event_id is given, the v2 mirror must record
    it so SSE consumers can build the reply chain."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat, summary = _open_thread_and_op(m)
    # First post a real speech event so we have a v2 event id.
    from app.behaviors.chat.conversation_schemas import SpeechActSubmitRequest
    chat.submit_speech(
        conversation_id=summary.id,
        request=SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="hi",
            addressed_to="@curator",
        ),
    )
    # Find the v2 event for that speech.
    from app.kernel.v2 import V2Repository
    from app.behaviors.chat.models import ChatConversationModel
    repo = V2Repository()
    with m["db"].session_scope() as s:
        v1_row = s.get(ChatConversationModel, summary.id)
        v2_op_id = v1_row.v2_operation_id
        assert v2_op_id, "v1 conversation should have a mirrored v2 op id"
        events = repo.list_events(s, operation_id=v2_op_id, limit=20)
        speech_evs = [e for e in events if e.kind.startswith("chat.speech.")]
        assert speech_evs, "expected at least one speech event"
        trigger_id = speech_evs[-1].id

    out = chat.emit_system_event(
        conversation_id=summary.id,
        kind="chat.system.nudge",
        addressed_to_handle="@curator",
        replies_to_v2_event_id=trigger_id,
        payload={"reason": "test"},
    )
    nudge_v2_id = out["v2_event_id"]
    assert nudge_v2_id
    # Read back the nudge event and check replies_to.
    with m["db"].session_scope() as s:
        from app.kernel.v2.models import OperationEventV2Model
        nudge = s.get(OperationEventV2Model, nudge_v2_id)
        assert nudge is not None
        assert nudge.replies_to_event_id == trigger_id
        assert nudge.kind == "chat.system.nudge"


def test_emit_system_event_does_not_count_toward_max_rounds(tmp_path, monkeypatch):
    """chat.system.nudge is OUTSIDE chat.speech.* so PolicyEngine's
    count_events(kind_prefix='chat.speech.') does not see it."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat, summary = _open_thread_and_op(m)
    chat.emit_system_event(
        conversation_id=summary.id,
        kind="chat.system.nudge",
        addressed_to_handle="@curator",
    )
    chat.emit_system_event(
        conversation_id=summary.id,
        kind="chat.system.nudge",
        addressed_to_handle="@designer",
    )
    from app.kernel.v2 import V2Repository
    from app.behaviors.chat.models import ChatConversationModel
    repo = V2Repository()
    with m["db"].session_scope() as s:
        v1_row = s.get(ChatConversationModel, summary.id)
        v2_op_id = v1_row.v2_operation_id
        n_speech = repo.count_events(
            s, operation_id=v2_op_id, kind_prefix="chat.speech.",
        )
    # Two nudges emitted but they don't show up under chat.speech.*
    assert n_speech == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
