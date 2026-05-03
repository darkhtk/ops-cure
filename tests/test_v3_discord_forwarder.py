"""v3 op speech events forwarded to Discord.

Pre-fix: agents posting via /v2/operations/{id}/events bypassed
Discord entirely — bridge stored the event but the parent thread
stayed silent. Operators couldn't see what their agents were
saying without polling /events.

Post-fix: append_event hooks the success path with a
fastapi BackgroundTask that posts a formatted message to the
chat thread's discord_thread_id via the existing thread_manager.
Best-effort: Discord errors never block the event write.

The test installs a stub thread_manager that records what would've
been posted, then drives speech events through the HTTP layer.
"""
from __future__ import annotations

import sys
import uuid
from typing import Any

from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv(
        "BRIDGE_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'b.db').as_posix()}",
    )
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.models import ChatThreadModel
    from app.main import app
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread, suffix="fwd"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()),
            guild_id="g",
            parent_channel_id="p",
            discord_thread_id=f"d-{suffix}",
            title=f"t-{suffix}",
            created_by="alice",
        )
        s.add(t)
        s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}
_DUMMY_SHA = "0123456789abcdef" * 4


class _RecordingThreadManager:
    """Captures (thread_id, text) pairs that the forwarder would
    have posted to Discord. Discord-disabled production
    thread_manager short-circuits inside post_message; here we
    let the calls through and just record."""
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def post_message(self, thread_id: str, text: str) -> list[tuple[str, str]]:
        self.calls.append((thread_id, text))
        return []


def _install_recorder(app) -> _RecordingThreadManager:
    rec = _RecordingThreadManager()
    app.state.services.thread_manager = rec
    return rec


def test_speech_event_forwards_to_discord_thread(tmp_path, monkeypatch):
    """A simple speech.claim ends up posted to the parent Discord
    thread with handle + kind + text."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="basic")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        rec = _install_recorder(m["app"])

        op = client.post("/v2/operations", json={
            "space_id": discord, "kind": "inquiry",
            "title": "fwd-test", "opener_actor_handle": "@alice",
        }).json()["id"]

        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.claim",
                "payload": {"text": "Hello from the agent."},
            },
        )
        assert r.status_code == 201, r.text

    # BackgroundTask runs after response; TestClient awaits it on context exit.
    assert len(rec.calls) >= 1
    thread_id, text = rec.calls[-1]
    assert thread_id == discord
    assert "@operator" in text
    assert "Hello from the agent." in text
    assert "[claim]" in text


def test_evidence_with_artifact_includes_artifact_line(tmp_path, monkeypatch):
    """speech.evidence with payload.artifact gets a 📎 footer in the
    Discord forward — uri + size + truncated sha so reviewers can
    spot the deliverable inline."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="art")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        rec = _install_recorder(m["app"])

        op = client.post("/v2/operations", json={
            "space_id": discord, "kind": "inquiry",
            "title": "fwd-art", "opener_actor_handle": "@alice",
        }).json()["id"]
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "wrote it",
                    "artifact": {
                        "kind": "code", "uri": "file:///x.html",
                        "sha256": _DUMMY_SHA, "mime": "text/html",
                        "size_bytes": 4096,
                    },
                },
            },
        )
        assert r.status_code == 201

    forwards = [c for c in rec.calls if "wrote it" in c[1]]
    assert len(forwards) == 1
    text = forwards[0][1]
    assert "📎 artifact" in text
    assert "file:///x.html" in text
    assert "4096" in text
    assert _DUMMY_SHA[:12] in text


def test_forwarder_is_best_effort_doesnt_break_event_write(tmp_path, monkeypatch):
    """Discord post raising an exception MUST NOT cause the event
    write to fail. The HTTP response still succeeds; the failure
    is logged."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="boom")

    class _BoomManager:
        async def post_message(self, *args, **kwargs):
            raise RuntimeError("simulated discord outage")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        m["app"].state.services.thread_manager = _BoomManager()

        op = client.post("/v2/operations", json={
            "space_id": discord, "kind": "inquiry",
            "title": "fwd-boom", "opener_actor_handle": "@alice",
        }).json()["id"]
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.claim",
                "payload": {"text": "should still 201 even if discord blew up"},
            },
        )
        # The event was accepted regardless of the forwarder failure.
        assert r.status_code == 201, r.text
