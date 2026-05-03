"""P9.7 / D3 — invented handle warning.

Pre-fix: ``expected_response.from_actor_handles`` accepted any
string. Reviewer agents seen in RPG / Unity smoke invented
handles like ``@autoplayer1`` / ``@auditor`` that mapped to no
registered actor — wasted obligation slots and confused routing.

Post-fix:
  - Default mode: log a WARN with the unknown handle list. Event
    write proceeds (caller may have a legitimate "future actor"
    pattern).
  - Strict mode (``BRIDGE_REQUIRE_KNOWN_HANDLES=1``): reject with
    HTTP 400 listing the offending handles.
"""
from __future__ import annotations

import logging
import sys
import uuid

from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch, *, strict: bool = False):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    if strict:
        monkeypatch.setenv("BRIDGE_REQUIRE_KNOWN_HANDLES", "1")
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


def _thread(db, Thread, suffix="d3"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}


def _open(client, *, discord):
    r = client.post("/v2/operations", json={
        "space_id": discord, "kind": "inquiry",
        "title": "handle probe", "opener_actor_handle": "@alice",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_default_soft_warns_on_unknown_handle(tmp_path, monkeypatch, caplog):
    """Default mode: unknown handles in expected_response.from_actor_handles
    are accepted but logged WARN."""
    m = _bootstrap(tmp_path, monkeypatch, strict=False)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="soft")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        with caplog.at_level(logging.WARNING):
            r = client.post(
                f"/v2/operations/{op}/events",
                json={
                    "actor_handle": "@alice",
                    "kind": "speech.question",
                    "payload": {"text": "who's there"},
                    "expected_response": {
                        "from_actor_handles": ["@autoplayer1", "@auditor"],
                    },
                },
            )
        assert r.status_code == 201, r.text
        assert any(
            "unknown actor handles" in rec.getMessage()
            and "@autoplayer1" in rec.getMessage()
            and "@auditor" in rec.getMessage()
            for rec in caplog.records
        )


def test_strict_mode_rejects_unknown_handle(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch, strict=True)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="strict")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@alice",
                "kind": "speech.question",
                "payload": {"text": "who's there"},
                "expected_response": {
                    "from_actor_handles": ["@autoplayer1"],
                },
            },
        )
        assert r.status_code == 400
        assert "@autoplayer1" in r.json()["detail"]


def test_known_handle_passes_in_strict_mode(tmp_path, monkeypatch):
    """Once an actor has spoken on the bridge they're registered.
    Inviting them by handle works in strict mode."""
    m = _bootstrap(tmp_path, monkeypatch, strict=True)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="known")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        # Get @reviewer registered by speaking once
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@reviewer",
                "kind": "speech.claim",
                "payload": {"text": "hi"},
            },
        )
        assert r.status_code == 201
        # Now invite them — should pass even in strict mode
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@alice",
                "kind": "speech.question",
                "payload": {"text": "thoughts?"},
                "expected_response": {"from_actor_handles": ["@reviewer"]},
            },
        )
        assert r.status_code == 201, r.text


def test_no_expected_response_no_warning(tmp_path, monkeypatch, caplog):
    """Events without expected_response don't trigger any handle
    validation noise."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="quiet")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        with caplog.at_level(logging.WARNING):
            r = client.post(
                f"/v2/operations/{op}/events",
                json={
                    "actor_handle": "@alice",
                    "kind": "speech.claim",
                    "payload": {"text": "no addressing"},
                },
            )
        assert r.status_code == 201, r.text
        assert not any(
            "unknown actor handles" in rec.getMessage()
            for rec in caplog.records
        )
