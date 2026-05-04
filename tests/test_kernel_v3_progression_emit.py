"""P13-2: ProgressionRunner emits chat.system.nudge / chat.speech.defer."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

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
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.kernel.presence import PresenceService
    from app.kernel.approvals import KernelApprovalService
    from app.kernel.v2 import V2Repository
    from app.kernel.v2.actor_service import ActorService
    from app.kernel.v2.progression_sweeper import (
        ProgressionRunner, ProgressionSweeper,
    )
    from app.services.remote_task_service import RemoteTaskService
    db.init_db()
    return locals()


def _open_op(m):
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


def _force_event_age(m, op_id, age_s):
    """Push every event of an op back by age_s seconds — simulates idle."""
    from app.kernel.v2.models import OperationEventV2Model
    from sqlalchemy import select
    from datetime import datetime, timedelta, timezone
    with m["db"].session_scope() as s:
        for ev in s.scalars(
            select(OperationEventV2Model).where(
                OperationEventV2Model.operation_id == op_id,
            )
        ):
            ev.created_at = (
                datetime.now(timezone.utc) - timedelta(seconds=age_s)
            )
        s.flush()


def test_runner_emits_nudge_when_decision_is_nudge(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    chat, summary = _open_op(m)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="please curate",
            addressed_to="@curator",
        ),
    )
    # Resolve v2 op_id; ensure the curator actor row exists so handle resolves.
    from app.behaviors.chat.models import ChatConversationModel
    with m["db"].session_scope() as s:
        v1_row = s.get(ChatConversationModel, summary.id)
        v2_op_id = v1_row.v2_operation_id
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    with m["db"].session_scope() as s:
        actors.ensure_actor_by_handle(s, handle="@curator")

    _force_event_age(m, v2_op_id, age_s=120)

    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    runner = m["ProgressionRunner"](
        sweeper=sweeper,
        session_scope=m["db"].session_scope,
        chat_service=chat,
    )
    runner._tick_once()

    # Verify a chat.system.nudge event landed on the op addressed to @curator.
    with m["db"].session_scope() as s:
        kinds = [e.kind for e in repo.list_events(s, operation_id=v2_op_id, limit=20)]
    assert kinds.count("chat.system.nudge") == 1, kinds


def test_runner_log_only_when_no_chat_service(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    chat, summary = _open_op(m)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="ping",
            addressed_to="@curator",
        ),
    )
    from app.behaviors.chat.models import ChatConversationModel
    with m["db"].session_scope() as s:
        v1_row = s.get(ChatConversationModel, summary.id)
        v2_op_id = v1_row.v2_operation_id
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    with m["db"].session_scope() as s:
        actors.ensure_actor_by_handle(s, handle="@curator")

    _force_event_age(m, v2_op_id, age_s=120)

    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    runner = m["ProgressionRunner"](
        sweeper=sweeper,
        session_scope=m["db"].session_scope,
        chat_service=None,  # log-only mode
    )
    runner._tick_once()

    with m["db"].session_scope() as s:
        kinds = [e.kind for e in repo.list_events(s, operation_id=v2_op_id, limit=20)]
    assert "chat.system.nudge" not in kinds, \
        "log-only mode must not emit nudge events"


def test_runner_emits_defer_after_max_retries(tmp_path, monkeypatch):
    """Three idle ticks on the same trigger ⇒ first two nudges, third
    is a defer escalation."""
    m = _bootstrap(tmp_path, monkeypatch)
    chat, summary = _open_op(m)
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="claim", content="please curate",
            addressed_to="@curator",
        ),
    )
    from app.behaviors.chat.models import ChatConversationModel
    with m["db"].session_scope() as s:
        v1_row = s.get(ChatConversationModel, summary.id)
        v2_op_id = v1_row.v2_operation_id
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    with m["db"].session_scope() as s:
        actors.ensure_actor_by_handle(s, handle="@curator")

    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    runner = m["ProgressionRunner"](
        sweeper=sweeper,
        session_scope=m["db"].session_scope,
        chat_service=chat,
    )

    # Tick 1: triggers a nudge. Force the only speech event back so it's idle.
    _force_event_age(m, v2_op_id, age_s=120)
    runner._tick_once()
    # Tick 2: now we have one nudge + the original speech, both pushed back.
    _force_event_age(m, v2_op_id, age_s=120)
    runner._tick_once()
    # Tick 3: two prior nudges → defer.
    _force_event_age(m, v2_op_id, age_s=120)
    runner._tick_once()

    with m["db"].session_scope() as s:
        kinds = [e.kind for e in repo.list_events(s, operation_id=v2_op_id, limit=50)]
    n_nudge = kinds.count("chat.system.nudge")
    n_defer = kinds.count("chat.speech.defer")
    assert n_nudge == 2, f"expected 2 nudges, got {n_nudge} (kinds={kinds})"
    assert n_defer == 1, f"expected 1 defer, got {n_defer} (kinds={kinds})"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
