"""F3: v1 conversation open/close mirrors to v2 Operation rows."""
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
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import ConversationOpenRequest
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.kernel.v2 import V2Repository
    db.init_db()
    return {
        "db": db,
        "ChatConversationService": ChatConversationService,
        "ConversationOpenRequest": ConversationOpenRequest,
        "ChatThreadModel": ChatThreadModel,
        "ChatConversationModel": ChatConversationModel,
        "V2Repository": V2Repository,
    }


def _make_thread(db_module, ChatThreadModel) -> str:
    """Insert a chat_threads row directly so we can drive the service."""
    with db_module.session_scope() as session:
        thread = ChatThreadModel(
            id=str(uuid.uuid4()),
            guild_id="g",
            parent_channel_id="p",
            discord_thread_id="d-thread-1",
            title="t",
            created_by="alice",
        )
        session.add(thread)
        session.flush()
        return thread.discord_thread_id


def test_open_inquiry_mirrors_to_v2_operation(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord_id = _make_thread(db, m["ChatThreadModel"])

    summary = svc.open_conversation(
        discord_thread_id=discord_id,
        request=m["ConversationOpenRequest"](
            kind="inquiry",
            title="Where are last week's logs?",
            intent="find logs",
            opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], summary.id)
        assert v1.v2_operation_id is not None

        op = repo.get_operation(session, v1.v2_operation_id)
        assert op.kind == "inquiry"
        assert op.title == "Where are last week's logs?"
        assert op.state == "open"

        meta = repo.operation_metadata(op)
        assert meta["v1_conversation_id"] == v1.id

        parts = repo.list_participants(session, operation_id=op.id)
        roles = sorted({p.role for p in parts})
        assert roles == ["addressed", "opener"]


def test_close_conversation_closes_v2_operation(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord_id = _make_thread(db, m["ChatThreadModel"])

    opened = svc.open_conversation(
        discord_thread_id=discord_id,
        request=m["ConversationOpenRequest"](
            kind="proposal",
            title="Adopt structured logging",
            opener_actor="alice",
        ),
    )

    svc.close_conversation(
        conversation_id=opened.id,
        closed_by="alice",
        resolution="accepted",
        summary="approved by team",
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], opened.id)
        op = repo.get_operation(session, v1.v2_operation_id)
        assert op.state == "closed"
        assert op.resolution == "accepted"
        assert op.resolution_summary == "approved by team"
        assert op.closed_by_actor_id is not None  # alice's actor
