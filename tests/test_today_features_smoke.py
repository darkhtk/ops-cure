"""End-to-end smoke test for the AI 협업룸 protocol body of work.

This file is the "did anything regress today?" check. Each function
exercises one PR's main invariant in isolation. They are deliberately
small and self-contained so a regression in any single PR surfaces
without dragging the whole protocol with it.

Coverage map (PR -> feature -> test fn):

  PR1  Conversation primitive          test_pr1_conversation_primitive
  PR2  Task <-> Conversation binding   test_pr2_task_conversation_binding
  PR3  Idle sweep + handoff            test_pr3_idle_sweep_and_handoff
  PR5  Resolution enum + dead code     test_pr5_resolution_enum_and_dead_code
  PR6  String-match authorization      test_pr6_close_authorization
  PR7  3-tier escalation + gauge       test_pr7_three_tier_escalation_and_gauge
  PR8  Kernel Operation promotion      test_pr8_kernel_operation_alias
  PR9  Kind filter + envelope helper   test_pr9_kind_filter_and_envelope_helper
  PR10 Lease contention                test_pr10_lease_contention

A single ``_smoke_env`` fixture-style helper bootstraps an in-process
chat stack against a tmp sqlite DB; each test gets a fresh env.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from conftest import FakeThreadManager, NAS_BRIDGE_ROOT


def _smoke_bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'smoke.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.db as db
    import app.behaviors.chat.service as chat_service_module
    import app.behaviors.chat.conversation_service as conversation_service_module
    import app.behaviors.chat.conversation_schemas as conversation_schemas_module
    import app.behaviors.chat.task_coordinator as task_coordinator_module
    import app.behaviors.chat.models as chat_models
    import app.kernel.operations as kernel_operations_module
    import app.kernel.events as kernel_events_module
    import app.kernel.presence as presence_module
    import app.kernel.approvals as approvals_module
    import app.models as models_module
    import app.services.remote_task_service as remote_task_service_module

    db.init_db()

    thread_manager = FakeThreadManager()
    chat_service = chat_service_module.ChatBehaviorService(thread_manager=thread_manager)
    presence = presence_module.PresenceService()
    approvals = approvals_module.KernelApprovalService()
    remote_task = remote_task_service_module.RemoteTaskService(
        presence_service=presence, kernel_approval_service=approvals,
    )
    conversation_service = conversation_service_module.ChatConversationService(
        remote_task_service=remote_task,
    )
    coordinator = task_coordinator_module.ChatTaskCoordinator(
        conversation_service=conversation_service, remote_task_service=remote_task,
    )

    async def open_thread():
        return await chat_service.create_chat_thread(
            guild_id="g", parent_channel_id="p", title="smoke",
            topic=None, created_by="alice",
        )

    return {
        "db": db,
        "chat_service": chat_service,
        "conversation_service": conversation_service,
        "coordinator": coordinator,
        "remote_task": remote_task,
        "schemas": conversation_schemas_module,
        "chat_models": chat_models,
        "kernel_operations": kernel_operations_module,
        "kernel_events": kernel_events_module,
        "models": models_module,
        "open_thread": lambda: asyncio.run(open_thread()),
    }


# --- PR1 -------------------------------------------------------------------


def test_pr1_conversation_primitive(tmp_path, monkeypatch):
    """PR1: thread creation bootstraps general; submit_speech stamps it
    on a ChatMessageModel row; open/speak/close on a non-general kind
    works end-to-end."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    chat_models = env["chat_models"]
    db = env["db"]

    thread = env["open_thread"]()

    # general bootstrapped
    with db.session_scope() as session:
        general = session.scalar(
            select(chat_models.ChatConversationModel)
            .where(chat_models.ChatConversationModel.thread_id == thread.id)
            .where(chat_models.ChatConversationModel.is_general.is_(True))
        )
        assert general is not None and general.state == "open"

    # casual chat lands on general with conversation_id stamped
    env["chat_service"].submit_participant_message(
        thread_id=thread.discord_thread_id,
        actor_name="alice", actor_kind="human",
        content="morning",
    )
    with db.session_scope() as session:
        msg = session.scalar(
            select(chat_models.ChatMessageModel)
            .where(chat_models.ChatMessageModel.thread_id == thread.id)
        )
        assert msg.conversation_id is not None
        assert msg.event_kind == "claim"

    # explicit inquiry: open -> speech -> close
    inquiry = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    env["conversation_service"].submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="bob", kind="answer", content="here",
        ),
    )
    closed = env["conversation_service"].close_conversation(
        conversation_id=inquiry.id, closed_by="alice", resolution="answered",
    )
    assert closed.state == "closed" and closed.resolution == "answered"


# --- PR2 -------------------------------------------------------------------


def test_pr2_task_conversation_binding(tmp_path, monkeypatch):
    """PR2: kind=task creates a bound RemoteTask; coordinator drives
    claim/heartbeat/evidence/complete; conversation auto-closes."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    thread = env["open_thread"]()

    task_conv = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="do X",
        ),
    )
    assert task_conv.bound_task_id is not None

    claimed = env["coordinator"].claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="codex", lease_seconds=120),
    )
    lease = claimed.task["current_assignment"]["lease_token"]
    env["coordinator"].heartbeat(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskHeartbeatRequest(
            actor_name="codex", lease_token=lease, phase="executing",
            files_read_count=1,
        ),
    )
    env["coordinator"].add_evidence(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskEvidenceRequest(
            actor_name="codex", lease_token=lease,
            kind="file_write", summary="patched",
        ),
    )
    completed = env["coordinator"].complete(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskCompleteRequest(
            actor_name="codex", lease_token=lease, summary="done",
        ),
    )
    assert completed.conversation.state == "closed"
    assert completed.conversation.resolution == "completed"
    assert completed.task["status"] == "completed"


# --- PR3 -------------------------------------------------------------------


def test_pr3_idle_sweep_and_handoff(tmp_path, monkeypatch):
    """PR3: sweep_idle emits a warning, transfer_owner moves owner_actor."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    chat_models = env["chat_models"]
    db = env["db"]
    thread = env["open_thread"]()

    proposal = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal", title="p", opener_actor="alice", owner_actor="alice",
        ),
    )
    # backdate to 35min so tier-1 (30min) fires
    backdated = datetime.now(timezone.utc) - timedelta(minutes=35)
    with db.session_scope() as session:
        row = session.get(chat_models.ChatConversationModel, proposal.id)
        row.created_at = backdated
        row.last_speech_at = backdated

    flagged = env["conversation_service"].sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    assert len(flagged) == 1 and flagged[0].idle_warning_count == 1

    transferred = env["conversation_service"].transfer_owner(
        conversation_id=proposal.id, by_actor="alice", new_owner="bob",
    )
    assert transferred.owner_actor == "bob"


# --- PR5 -------------------------------------------------------------------


def test_pr5_resolution_enum_and_dead_code(tmp_path, monkeypatch):
    """PR5: closing an inquiry with a proposal-shape resolution is
    rejected; the dead `address` speech kind is no longer accepted."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    thread = env["open_thread"]()
    conv_module = sys.modules["app.behaviors.chat.conversation_service"]

    inquiry = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="q", opener_actor="alice",
        ),
    )
    with pytest.raises(conv_module.ChatConversationStateError):
        env["conversation_service"].close_conversation(
            conversation_id=inquiry.id, closed_by="alice", resolution="accepted",
        )

    # `address` should no longer be a SpeechKind literal -- pydantic rejects.
    with pytest.raises(Exception):  # noqa: BLE001
        schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="address", content="...",
        )


# --- PR6 -------------------------------------------------------------------


def test_pr6_close_authorization(tmp_path, monkeypatch):
    """PR6: random actor cannot close someone else's conversation."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    thread = env["open_thread"]()
    conv_module = sys.modules["app.behaviors.chat.conversation_service"]

    inquiry = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    with pytest.raises(conv_module.ChatConversationStateError):
        env["conversation_service"].close_conversation(
            conversation_id=inquiry.id, closed_by="mallory", resolution="dropped",
        )


# --- PR7 -------------------------------------------------------------------


def test_pr7_three_tier_escalation_and_gauge(tmp_path, monkeypatch):
    """PR7: a 25h-stale conversation auto-abandons; unaddressed_speech_count
    counts off-turn speech."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    chat_models = env["chat_models"]
    db = env["db"]
    thread = env["open_thread"]()

    proposal = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="proposal", title="p", opener_actor="alice",
        ),
    )
    backdated = datetime.now(timezone.utc) - timedelta(minutes=25 * 60)
    with db.session_scope() as session:
        row = session.get(chat_models.ChatConversationModel, proposal.id)
        row.created_at = backdated
        row.last_speech_at = backdated

    flagged = env["conversation_service"].sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    assert any(f.resolution == "abandoned" and f.idle_warning_count == 3 for f in flagged)

    # Soft quota gauge: addressed_to=bob, then carol speaks unaddressed.
    inquiry = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice", addressed_to="bob",
        ),
    )
    env["conversation_service"].submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="carol", kind="claim", content="butting in",
        ),
    )
    detail = env["conversation_service"].get_conversation(conversation_id=inquiry.id)
    assert detail.conversation.unaddressed_speech_count == 1


# --- PR8 -------------------------------------------------------------------


def test_pr8_kernel_operation_alias(tmp_path, monkeypatch):
    """PR8: kernel.operations.OperationModel is the same class as
    app.models.RemoteTaskModel; new code can import from kernel/."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    kernel_ops = env["kernel_operations"]
    models = env["models"]

    assert kernel_ops.OperationModel is models.RemoteTaskModel
    assert kernel_ops.OperationAssignmentModel is models.RemoteTaskAssignmentModel
    assert kernel_ops.OperationHeartbeatModel is models.RemoteTaskHeartbeatModel
    assert kernel_ops.OperationEvidenceModel is models.RemoteTaskEvidenceModel
    assert kernel_ops.OperationApprovalModel is models.RemoteTaskApprovalModel
    assert kernel_ops.OperationNoteModel is models.RemoteTaskNoteModel

    # Underlying tables unchanged so existing rows remain readable.
    assert kernel_ops.OperationModel.__tablename__ == "remote_tasks"

    # The kernel-vocab service entry point also resolves.
    from app.kernel.operation_service import KernelOperationService
    from app.services.remote_task_service import RemoteTaskService
    assert KernelOperationService is RemoteTaskService


# --- PR9 -------------------------------------------------------------------


def test_pr9_kind_filter_and_envelope_helper(tmp_path, monkeypatch):
    """PR9: get_conversation(kinds=[...]) filters; kernel.events
    helpers (make_message_envelope, publish_envelope) are exposed."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    kernel_events = env["kernel_events"]
    thread = env["open_thread"]()

    # helpers are part of the public kernel.events surface
    assert hasattr(kernel_events, "make_message_envelope")
    assert hasattr(kernel_events, "publish_envelope")

    inquiry = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    env["conversation_service"].submit_speech(
        conversation_id=inquiry.id,
        request=schemas.SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="anyone home?",
        ),
    )
    env["conversation_service"].close_conversation(
        conversation_id=inquiry.id, closed_by="alice", resolution="dropped",
    )

    # filter by event_kind
    only_close = env["conversation_service"].get_conversation(
        conversation_id=inquiry.id, kinds=["chat.conversation.closed"],
    )
    assert len(only_close.recent_speech) == 1
    assert only_close.recent_speech[0].kind == "chat.conversation.closed"

    only_speech = env["conversation_service"].get_conversation(
        conversation_id=inquiry.id, kinds=["chat.speech.claim"],
    )
    assert len(only_speech.recent_speech) == 1
    # SpeechActSummary strips the chat.speech. prefix; lifecycle events
    # (chat.conversation.*) keep their full event_kind.
    assert only_speech.recent_speech[0].kind == "claim"


# --- PR10 ------------------------------------------------------------------


def test_pr10_lease_contention(tmp_path, monkeypatch):
    """PR10: a second actor cannot claim while the first holds the
    lease; expiring the lease lets the second actor take over."""
    env = _smoke_bootstrap(tmp_path, monkeypatch)
    schemas = env["schemas"]
    models = env["models"]
    db = env["db"]
    thread = env["open_thread"]()

    task_conv = env["conversation_service"].open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=schemas.ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="x",
        ),
    )
    response_a = env["coordinator"].claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="A", lease_seconds=30),
    )
    with pytest.raises(Exception):  # noqa: BLE001
        env["coordinator"].claim(
            conversation_id=task_conv.id,
            request=schemas.ChatTaskClaimRequest(actor_name="B", lease_seconds=30),
        )

    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    assignment_id = response_a.task["current_assignment"]["id"]
    with db.session_scope() as session:
        assignment = session.get(models.RemoteTaskAssignmentModel, assignment_id)
        assignment.lease_expires_at = expired
        assignment.status = "released"
        assignment.released_at = expired
        lease = session.scalar(
            select(models.ResourceLeaseModel)
            .where(models.ResourceLeaseModel.resource_kind == "remote_task")
            .where(models.ResourceLeaseModel.resource_id == task_conv.bound_task_id)
        )
        if lease is not None:
            lease.expires_at = expired
            lease.released_at = expired
            lease.status = "released"

    response_b = env["coordinator"].claim(
        conversation_id=task_conv.id,
        request=schemas.ChatTaskClaimRequest(actor_name="B", lease_seconds=30),
    )
    assert response_b.task["status"] == "claimed"
    assert response_b.conversation.owner_actor == "B"
