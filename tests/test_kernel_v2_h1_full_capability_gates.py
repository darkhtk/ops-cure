"""H1: capability 가 모든 entry point 에서 정확하게 검사됨."""
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
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import (
        ChatConversationService, ChatActorIdentityError,
    )
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
        ChatTaskClaimRequest, ChatTaskEvidenceRequest,
        ChatTaskApprovalRequest, ChatTaskApprovalResolveRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.behaviors.chat.task_coordinator import ChatTaskCoordinator
    from app.kernel.presence import PresenceService
    from app.kernel.approvals import KernelApprovalService
    from app.kernel.v2 import (
        CapabilityService, make_per_capability_authorizer,
        CAP_CONVERSATION_OPEN, CAP_CONVERSATION_CLOSE_OPENER,
        CAP_CONVERSATION_HANDOFF, CAP_SPEECH_SUBMIT,
        CAP_TASK_CLAIM, CAP_TASK_COMPLETE, CAP_TASK_FAIL,
        CAP_TASK_APPROVE_DESTRUCTIVE,
    )
    from app.services.remote_task_service import RemoteTaskService
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread, suffix="1"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def test_default_actor_can_open_speak_close(tmp_path, monkeypatch):
    """Sanity: default ai/human actor goes through all primary gates."""
    m = _bootstrap(tmp_path, monkeypatch)
    cap = m["CapabilityService"]()
    svc = m["ChatConversationService"](
        capability_authorizer=m["make_per_capability_authorizer"](cap),
    )
    discord = _thread(m["db"], m["ChatThreadModel"])

    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x", opener_actor="alice",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="hi",
        ),
    )
    svc.close_conversation(
        conversation_id=summary.id, closed_by="alice",
        resolution="answered",
    )


def test_revoking_conversation_open_blocks_open(tmp_path, monkeypatch):
    """Explicitly revoke CAP_CONVERSATION_OPEN -> open is denied,
    submit_speech / close still independent."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    # Provision alice with explicit caps EXCLUDING open
    with db.session_scope() as session:
        cap.grant(
            session, actor_handle="@alice",
            capabilities=[
                m["CAP_SPEECH_SUBMIT"], m["CAP_CONVERSATION_CLOSE_OPENER"],
            ],
        )
    svc = m["ChatConversationService"](
        capability_authorizer=m["make_per_capability_authorizer"](cap),
    )
    discord = _thread(db, m["ChatThreadModel"])
    with pytest.raises(m["ChatActorIdentityError"]) as exc:
        svc.open_conversation(
            discord_thread_id=discord,
            request=m["ConversationOpenRequest"](
                kind="inquiry", title="x", opener_actor="alice",
            ),
        )
    assert "conversation.open" in str(exc.value)


def test_revoking_speech_submit_blocks_speak(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    # provision alice with default minus speech.submit
    with db.session_scope() as session:
        cap.grant(
            session, actor_handle="@alice",
            capabilities=[
                m["CAP_CONVERSATION_OPEN"], m["CAP_CONVERSATION_CLOSE_OPENER"],
            ],
        )
    svc = m["ChatConversationService"](
        capability_authorizer=m["make_per_capability_authorizer"](cap),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x", opener_actor="alice",
        ),
    )
    with pytest.raises(m["ChatActorIdentityError"]) as exc:
        svc.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                kind="claim", actor_name="alice", content="hi",
            ),
        )
    assert "speech.submit" in str(exc.value)


def test_revoking_close_opener_blocks_close(tmp_path, monkeypatch):
    """alice opens but lacks CLOSE_OPENER cap -> close is rejected."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    with db.session_scope() as session:
        cap.grant(
            session, actor_handle="@alice",
            capabilities=[
                m["CAP_CONVERSATION_OPEN"], m["CAP_SPEECH_SUBMIT"],
            ],
        )
    svc = m["ChatConversationService"](
        capability_authorizer=m["make_per_capability_authorizer"](cap),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x", opener_actor="alice",
        ),
    )
    with pytest.raises(m["ChatActorIdentityError"]) as exc:
        svc.close_conversation(
            conversation_id=summary.id, closed_by="alice",
            resolution="answered",
        )
    assert "conversation.close.opener" in str(exc.value)


def test_approve_destructive_requires_explicit_grant(tmp_path, monkeypatch):
    """Default capabilities exclude task.approve.destructive. An ai or
    human actor cannot approve a destructive request unless explicitly
    granted. denied is unrestricted (anyone with conversation rights)."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    cap_authorizer = m["make_per_capability_authorizer"](cap)
    remote = m["RemoteTaskService"](
        presence_service=m["PresenceService"](),
        kernel_approval_service=m["KernelApprovalService"](),
    )
    svc = m["ChatConversationService"](
        capability_authorizer=cap_authorizer,
        remote_task_service=remote,
    )
    coord = m["ChatTaskCoordinator"](
        conversation_service=svc,
        remote_task_service=remote,
    )
    discord = _thread(db, m["ChatThreadModel"])
    svc.ensure_general(discord_thread_id=discord)
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="task", title="t", objective="obj",
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
    coord.add_evidence(
        conversation_id=summary.id,
        request=m["ChatTaskEvidenceRequest"](
            actor_name="claude-pca", lease_token=lease,
            kind="screenshot", summary="prep",
            payload={},
        ),
    )
    coord.request_approval(
        conversation_id=summary.id,
        request=m["ChatTaskApprovalRequest"](
            actor_name="claude-pca", lease_token=lease,
            reason="risky deploy",
        ),
    )

    # alice doesn't have approve.destructive (default ai/human sets exclude
    # it). Approving must raise.
    with pytest.raises(m["ChatActorIdentityError"]) as exc:
        coord.resolve_approval(
            conversation_id=summary.id,
            request=m["ChatTaskApprovalResolveRequest"](
                resolved_by="alice", resolution="approved",
            ),
        )
    assert "task.approve.destructive" in str(exc.value)

    # Now grant alice the cap -- approval succeeds.
    with db.session_scope() as session:
        cap.grant(
            session, actor_handle="@alice",
            capabilities=[m["CAP_TASK_APPROVE_DESTRUCTIVE"]],
        )
    coord.resolve_approval(
        conversation_id=summary.id,
        request=m["ChatTaskApprovalResolveRequest"](
            resolved_by="alice", resolution="approved",
        ),
    )


def test_legacy_2arg_authorizer_still_works(tmp_path, monkeypatch):
    """Back-compat: the old (ctx, actor) -> bool signature is preserved
    when the new capability_authorizer slot is empty."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _thread(db, m["ChatThreadModel"])
    seen: list[tuple] = []

    def legacy(ctx, actor):
        seen.append((ctx, actor))
        return actor != "rejected-actor"

    svc = m["ChatConversationService"](actor_authorizer=legacy)
    # default actor goes through
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x", opener_actor="alice",
        ),
    )
    # rejected actor blocks (legacy authorizer is cap-blind so any gate fails)
    with pytest.raises(m["ChatActorIdentityError"]):
        svc.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                kind="claim", actor_name="rejected-actor", content="x",
            ),
        )
    assert len(seen) >= 2


def test_handoff_requires_handoff_capability(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    # alice gets everything except CONVERSATION_HANDOFF
    with db.session_scope() as session:
        cap.grant(
            session, actor_handle="@alice",
            capabilities=[
                m["CAP_CONVERSATION_OPEN"], m["CAP_SPEECH_SUBMIT"],
                m["CAP_CONVERSATION_CLOSE_OPENER"],
            ],
        )
    svc = m["ChatConversationService"](
        capability_authorizer=m["make_per_capability_authorizer"](cap),
    )
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x", opener_actor="alice",
        ),
    )
    with pytest.raises(m["ChatActorIdentityError"]) as exc:
        svc.transfer_owner(
            conversation_id=summary.id,
            by_actor="alice", new_owner="bob",
        )
    assert "conversation.handoff" in str(exc.value)
