"""F11: SDK client + AgentRuntime end-to-end against the live ASGI app.

Drives BridgeV2Client through httpx WSGITransport against the FastAPI
app instance so the SDK is exercised without a real server.
"""
from __future__ import annotations

import sys
import uuid

import httpx

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
    from app.main import app
    from app.agent_sdk import AgentRuntime, BridgeV2Client, IncomingEvent
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


def _make_client(app, handle: str, *, lifespan: bool = False):
    """Build a BridgeV2Client wired to FastAPI's TestClient (sync ASGI).

    When ``lifespan=True`` we enter the app lifespan (and the caller
    must call ``.close()`` on the returned client to release it). v2
    routes don't need lifespan because they don't touch
    ``app.state.services``; the chat / API routes do, so tests that
    call submit_speech etc. need lifespan=True.
    """
    from fastapi.testclient import TestClient
    test_http = TestClient(app, base_url="http://testserver")
    if lifespan:
        test_http.__enter__()
    test_http.headers.update({
        "Authorization": "Bearer t",
        "X-Bridge-Client-Id": handle.lstrip("@"),
    })
    from app.agent_sdk import BridgeV2Client
    c = BridgeV2Client(
        base_url="http://testserver", bearer_token="t", actor_handle=handle,
    )
    c._http.close()
    c._http = test_http
    c._test_client = test_http  # so close can release lifespan
    c._lifespan_active = lifespan
    return c


def test_client_get_inbox_through_asgi(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="q", opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )
    client = _make_client(m["app"], "@claude-pca")
    body = client.get_inbox(state="open")
    assert body["actor_handle"] == "@claude-pca"
    assert len(body["items"]) == 1
    assert body["items"][0]["kind"] == "inquiry"


def test_runtime_dispatches_events_and_advances_cursor(tmp_path, monkeypatch):
    """Runtime polls inbox, runs handler on each event, and the next
    tick yields zero events because mark_seen advanced the cursor."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="q", opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice",
            content="what?", addressed_to="claude-pca",
        ),
    )
    client = _make_client(m["app"], "@claude-pca")
    seen: list = []

    def handler(event, _client):
        seen.append((event.kind, event.seq))

    runtime = m["AgentRuntime"](client, handler, poll_interval_seconds=0)
    n1 = runtime.run_once()
    assert n1 >= 1
    # at least the question event was seen
    assert any(k == "chat.speech.question" for k, _ in seen)
    # second tick: cursor advanced -> no new events
    seen.clear()
    n2 = runtime.run_once()
    assert n2 == 0
    assert seen == []


def test_runtime_handler_can_send_reply_through_client(tmp_path, monkeypatch):
    """Handler reuses the same client to send a reply; the reply lands
    as a v2 OperationEvent in the same operation."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="q", opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice",
            content="2+2?", addressed_to="claude-pca",
        ),
    )

    client = _make_client(m["app"], "@claude-pca", lifespan=True)

    def handler(event, c):
        if event.kind != "chat.speech.question":
            return
        c.append_event(
            event.operation_id,
            kind="speech.claim",
            text=f"echo:{event.payload.get('text')}",
        )

    try:
        runtime = m["AgentRuntime"](client, handler, poll_interval_seconds=0)
        runtime.run_once()
    finally:
        client._test_client.__exit__(None, None, None)

    # verify v2 has the agent's reply
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id
    body = client.list_events(op_id)
    contents = [e["payload"].get("text", "") for e in body["events"]]
    assert any(c.startswith("echo:") for c in contents)
