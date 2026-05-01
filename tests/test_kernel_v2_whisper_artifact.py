"""F6: whisper (private_to) + artifact dual-write."""
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
        ConversationOpenRequest, SpeechActSubmitRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.kernel.v2 import V2Repository
    db.init_db()
    return locals() | {"db": db}


def _make_thread(db, Thread):
    with db.session_scope() as session:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        session.add(t)
        session.flush()
        return t.discord_thread_id


def test_whisper_lands_with_private_to_set_in_v2(tmp_path, monkeypatch):
    """submit_speech with private_to_actors -> v2 event has the
    private_to_actor_ids list pointing at those actors' rows."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _make_thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="proposal", title="adopt logging", opener_actor="alice",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice",
            content="bob, between us this is risky",
            private_to_actors=["bob"],
        ),
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], summary.id)
        events = repo.list_events(session, operation_id=v1.v2_operation_id)
        speech = [e for e in events if e.kind == "chat.speech.claim"]
        assert len(speech) == 1
        priv = repo.event_private_to(speech[0])
        assert priv is not None
        bob = repo.get_actor_by_handle(session, "@bob")
        assert priv == [bob.id]


def test_evidence_artifact_attaches_to_v2_event(tmp_path, monkeypatch):
    """A task evidence carrying an artifact dict ends up as a row in
    operation_artifacts_v2 keyed by the v2 evidence event id."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    # Build a task conversation through the chat service so the bound
    # task + lease are real.
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, ChatTaskClaimRequest, ChatTaskEvidenceRequest,
    )
    from app.services.remote_task_service import RemoteTaskService
    from app.kernel.presence import PresenceService
    from app.kernel.approvals import KernelApprovalService
    from app.behaviors.chat.task_coordinator import ChatTaskCoordinator

    remote_task = RemoteTaskService(
        presence_service=PresenceService(),
        kernel_approval_service=KernelApprovalService(),
    )
    chat = m["ChatConversationService"](remote_task_service=remote_task)
    coord = ChatTaskCoordinator(
        conversation_service=chat,
        remote_task_service=remote_task,
    )
    discord = _make_thread(db, m["ChatThreadModel"])
    # Pre-create general so the open_conversation session does no
    # writes prior to remote_task_service.create_task -- otherwise
    # SQLite holds an exclusive lock that the inner create_task can't
    # acquire (real production calls ensure_general earlier, in the
    # chat thread bootstrap).
    chat.ensure_general(discord_thread_id=discord)
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=ConversationOpenRequest(
            kind="task", title="patch middleware",
            objective="replace legacy session token storage",
            opener_actor="alice",
        ),
    )
    # claim + evidence with artifact
    claim_resp = coord.claim(
        conversation_id=summary.id,
        request=ChatTaskClaimRequest(actor_name="claude-pca", lease_seconds=120),
    )
    lease_token = claim_resp.task["current_assignment"]["lease_token"]

    coord.add_evidence(
        conversation_id=summary.id,
        request=ChatTaskEvidenceRequest(
            actor_name="claude-pca", lease_token=lease_token,
            kind="screenshot", summary="prod console error",
            payload={"artifact": {
                "kind": "screenshot",
                "uri": "nas://volume1/artifacts/abc.png",
                "sha256": "deadbeef" * 8,
                "mime": "image/png",
                "size_bytes": 4096,
                "label": "console error",
            }},
        ),
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], summary.id)
        artifacts = repo.list_artifacts_for_operation(session, operation_id=v1.v2_operation_id)
        assert len(artifacts) == 1
        a = artifacts[0]
        assert a.kind == "screenshot"
        assert a.uri.endswith("abc.png")
        assert a.sha256 == "deadbeef" * 8
        assert a.label == "console error"
