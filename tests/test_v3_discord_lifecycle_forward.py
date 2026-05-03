"""P9.5 — Discord forwarder coverage extends to op lifecycle.

The Phase 7 forwarder (T1.2 follow-up) hooked only the speech
event path. open + close lifecycle markers were invisible to
operators watching the parent Discord thread.

Post-fix:
  - ``POST /v2/operations`` (open) emits a ``📣 op opened`` line
    via thread_manager.post_message in a BackgroundTask.
  - ``POST /v2/operations/{id}/close`` emits a ``✅ op closed``
    line summarizing resolution + event/artifact count.

Best-effort: Discord-disabled mode short-circuits inside
post_message; exceptions never break the HTTP write.
"""
from __future__ import annotations

import sys
import uuid

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


def _thread(db, Thread, suffix="lc"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def post_message(self, thread_id: str, text: str):
        self.calls.append((thread_id, text))
        return []


def test_open_forwards_to_discord(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="open")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        rec = _Recorder()
        m["app"].state.services.thread_manager = rec

        r = client.post("/v2/operations", json={
            "space_id": discord, "kind": "inquiry",
            "title": "lifecycle probe", "opener_actor_handle": "@alice",
            "policy": {"close_policy": "quorum", "min_ratifiers": 2},
        })
        assert r.status_code == 201, r.text

    forwards = [c for c in rec.calls if "op opened" in c[1]]
    assert len(forwards) == 1
    text = forwards[0][1]
    assert "lifecycle probe" in text
    assert "@alice" in text
    assert "quorum" in text


def test_close_forwards_to_discord(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="close")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        rec = _Recorder()
        m["app"].state.services.thread_manager = rec

        op_id = client.post("/v2/operations", json={
            "space_id": discord, "kind": "inquiry",
            "title": "to-close", "opener_actor_handle": "@alice",
        }).json()["id"]
        r = client.post(f"/v2/operations/{op_id}/close", json={
            "actor_handle": "@alice",
            "resolution": "answered",
            "summary": "test wrap",
        })
        assert r.status_code == 200, r.text

    forwards = [c for c in rec.calls if "op closed" in c[1]]
    assert len(forwards) == 1
    text = forwards[0][1]
    assert "to-close" in text
    assert "answered" in text
    assert "@alice" in text
    assert "test wrap" in text


def test_open_close_forwarder_failures_are_swallowed(tmp_path, monkeypatch):
    """Discord errors must not break either /open or /close."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="boom")

    class _Boom:
        async def post_message(self, *a, **kw):
            raise RuntimeError("simulated discord outage")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        m["app"].state.services.thread_manager = _Boom()

        r = client.post("/v2/operations", json={
            "space_id": discord, "kind": "inquiry",
            "title": "boom-open", "opener_actor_handle": "@alice",
        })
        assert r.status_code == 201, r.text
        op_id = r.json()["id"]

        r = client.post(f"/v2/operations/{op_id}/close", json={
            "actor_handle": "@alice", "resolution": "answered", "summary": "x",
        })
        assert r.status_code == 200, r.text
