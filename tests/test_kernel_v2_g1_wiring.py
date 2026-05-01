"""G1: CapabilityService + OperationStateMachine wired into the chat path."""
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
        ChatConversationService, ChatActorIdentityError, ChatConversationStateError,
    )
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.kernel.v2 import (
        CapabilityService, ActorService, V2Repository,
        CAP_SPEECH_SUBMIT, CAP_CONVERSATION_OPEN,
        make_capability_authorizer,
        StateMachineError,
    )
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


def test_capability_authorizer_allows_default_actor(tmp_path, monkeypatch):
    """A fresh actor (auto-provisioned with default ai caps) gets
    speech.submit through the wired authorizer."""
    m = _bootstrap(tmp_path, monkeypatch)
    cap = m["CapabilityService"]()
    authorizer = m["make_capability_authorizer"](cap, capability=m["CAP_SPEECH_SUBMIT"])
    svc = m["ChatConversationService"](actor_authorizer=authorizer)
    discord = _thread(m["db"], m["ChatThreadModel"])

    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="logs?", opener_actor="alice",
        ),
    )
    # And submit_speech also passes
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="hi",
        ),
    )


def test_capability_authorizer_rejects_revoked_actor(tmp_path, monkeypatch):
    """If a specific actor has speech.submit revoked, submit_speech
    raises ChatActorIdentityError before any v1 row is written."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    # Pre-provision @mute as a no-permission actor explicitly (empty list
    # is treated as "explicit grant" -- defaults stop applying).
    with db.session_scope() as s:
        actor_service = m["ActorService"]()
        actor_service.ensure_actor_by_handle(s, handle="@mute")
        cap.grant(s, actor_handle="@mute", capabilities=[m["CAP_CONVERSATION_OPEN"]])
        cap.revoke(s, actor_handle="@mute", capabilities=[m["CAP_SPEECH_SUBMIT"]])
    authorizer = m["make_capability_authorizer"](cap, capability=m["CAP_SPEECH_SUBMIT"])
    svc = m["ChatConversationService"](actor_authorizer=authorizer)
    discord = _thread(m["db"], m["ChatThreadModel"])

    # alice opens; mute is rejected at submit_speech below.
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="q", opener_actor="alice",
        ),
    )
    # but cannot submit speech
    with pytest.raises(m["ChatActorIdentityError"]):
        svc.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                kind="claim", actor_name="mute", content="should fail",
            ),
        )


def test_state_machine_blocks_invalid_resolution_at_v2_close(tmp_path, monkeypatch):
    """v1 ALLOWED_RESOLUTIONS_BY_KIND already blocks bad resolutions
    at the chat layer -- the v2 state machine assertion is the second
    safety net inside the mirror. We verify the v1 layer's error is
    what surfaces (so v1 is still the primary gate)."""
    m = _bootstrap(tmp_path, monkeypatch)
    svc = m["ChatConversationService"]()  # no authorizer needed for this test
    discord = _thread(m["db"], m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="proposal", title="p", opener_actor="alice",
        ),
    )
    with pytest.raises(m["ChatConversationStateError"]):
        svc.close_conversation(
            conversation_id=summary.id, closed_by="alice",
            resolution="answered",  # answered is inquiry vocab, not proposal
        )


def test_state_machine_allows_v1_vocab_resolutions(tmp_path, monkeypatch):
    """All v1 resolutions for proposal pass through the v2 state
    machine without raising -- vocab is in sync."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    for resolution in ["accepted", "rejected", "withdrawn", "superseded"]:
        opened = svc.open_conversation(
            discord_thread_id=discord,
            request=m["ConversationOpenRequest"](
                kind="proposal", title=f"p-{resolution}", opener_actor="alice",
            ),
        )
        # Should not raise
        svc.close_conversation(
            conversation_id=opened.id, closed_by="alice",
            resolution=resolution,
        )
        # And v2 op state is closed with the same resolution
        with db.session_scope() as s:
            v1 = s.get(m["ChatConversationModel"], opened.id)
            repo = m["V2Repository"]()
            op = repo.get_operation(s, v1.v2_operation_id)
            assert op.state == "closed"
            assert op.resolution == resolution
