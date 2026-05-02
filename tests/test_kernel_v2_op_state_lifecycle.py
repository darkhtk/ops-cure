"""#2 fix: task lifecycle 가 v2 operations_v2.state 까지 흐른다."""
from __future__ import annotations

import sys
import uuid

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest,
        ChatTaskClaimRequest, ChatTaskEvidenceRequest,
        ChatTaskApprovalRequest, ChatTaskApprovalResolveRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.behaviors.chat.task_coordinator import ChatTaskCoordinator
    from app.kernel.presence import PresenceService
    from app.kernel.approvals import KernelApprovalService
    from app.kernel.v2 import V2Repository
    from app.services.remote_task_service import RemoteTaskService
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def _setup_task(m):
    """Open a task conversation and return (op_id, conv_id, lease_token, coord)."""
    db = m["db"]
    remote_task = m["RemoteTaskService"](
        presence_service=m["PresenceService"](),
        kernel_approval_service=m["KernelApprovalService"](),
    )
    chat = m["ChatConversationService"](remote_task_service=remote_task)
    coord = m["ChatTaskCoordinator"](
        conversation_service=chat,
        remote_task_service=remote_task,
    )
    discord = _thread(db, m["ChatThreadModel"])
    chat.ensure_general(discord_thread_id=discord)
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="task", title="t",
            objective="do the thing",
            opener_actor="alice",
        ),
    )
    claim = coord.claim(
        conversation_id=summary.id,
        request=m["ChatTaskClaimRequest"](
            actor_name="claude-pca", lease_seconds=300,
        ),
    )
    lease = claim.task["current_assignment"]["lease_token"]
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id
    return op_id, summary.id, lease, coord


def test_claim_transitions_v2_op_state_to_claimed(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id, _, _, _ = _setup_task(m)
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        op = repo.get_operation(s, op_id)
        assert op.state == "claimed"


def test_first_evidence_transitions_to_executing(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id, conv_id, lease, coord = _setup_task(m)
    coord.add_evidence(
        conversation_id=conv_id,
        request=m["ChatTaskEvidenceRequest"](
            actor_name="claude-pca", lease_token=lease,
            kind="screenshot", summary="canary deploy ok",
            payload={},
        ),
    )
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        op = repo.get_operation(s, op_id)
        assert op.state == "executing"


def test_approval_request_blocks_then_resolve_unblocks(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id, conv_id, lease, coord = _setup_task(m)
    # Move to executing first via evidence (state machine wants
    # executing -> blocked_approval).
    coord.add_evidence(
        conversation_id=conv_id,
        request=m["ChatTaskEvidenceRequest"](
            actor_name="claude-pca", lease_token=lease,
            kind="screenshot", summary="prepping",
            payload={},
        ),
    )
    coord.request_approval(
        conversation_id=conv_id,
        request=m["ChatTaskApprovalRequest"](
            actor_name="claude-pca", lease_token=lease,
            reason="destructive op",
        ),
    )
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        op = repo.get_operation(s, op_id)
        assert op.state == "blocked_approval"

    coord.resolve_approval(
        conversation_id=conv_id,
        request=m["ChatTaskApprovalResolveRequest"](
            resolved_by="alice", resolution="approved",
        ),
    )
    with m["db"].session_scope() as s:
        op = repo.get_operation(s, op_id)
        assert op.state == "executing"


def test_inbox_state_filter_now_returns_executing_ops(tmp_path, monkeypatch):
    """The whole point of #2: inbox filter by op state is no longer
    silently empty for executing tasks."""
    m = _bootstrap(tmp_path, monkeypatch)
    op_id, conv_id, lease, coord = _setup_task(m)
    coord.add_evidence(
        conversation_id=conv_id,
        request=m["ChatTaskEvidenceRequest"](
            actor_name="claude-pca", lease_token=lease,
            kind="file_write", summary="patched",
            payload={},
        ),
    )
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        actor = repo.get_actor_by_handle(s, "@claude-pca")
        ops = repo.operations_for_actor(
            s, actor_id=actor.id, state="executing",
        )
        assert any(op.id == op_id for op, role in ops)
