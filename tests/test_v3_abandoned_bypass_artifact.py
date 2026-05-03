"""P10.4 — `requires_artifact` gate bypassed for terminal-failure
resolutions (abandoned, cancelled, failed, withdrawn, superseded,
dropped).

Pre-fix observation (Unity arcade smoke 2026-05-04): when the user
called `/close` with `resolution=abandoned` to wrap up an op that
had no artifact, the bridge held the close because
`requires_artifact=true`. Alice had to fabricate evidence (attach a
half-built exe) just to satisfy the gate — exactly the wrong shape:
the gate should be enforcing "no FALSE success", not "no abandon".

Post-fix: the artifact gate fires only when the resolution is in
the success vocabulary. Abandon paths bypass.
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


def _thread(db, Thread, suffix="p10_4"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}


def _open(client, *, discord, kind="inquiry"):
    r = client.post("/v2/operations", json={
        "space_id": discord, "kind": kind,
        "title": "p10.4 probe", "opener_actor_handle": "@alice",
        "policy": {"requires_artifact": True},
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _try_close(client, op, *, resolution):
    return client.post(
        f"/v2/operations/{op}/close",
        json={
            "actor_handle": "@alice",
            "resolution": resolution,
            "summary": "p10.4 probe",
        },
    )


def test_abandoned_close_bypasses_requires_artifact(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="abandoned")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        r = _try_close(client, op, resolution="abandoned")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "closed"
        assert r.json()["resolution"] == "abandoned"


def test_dropped_close_bypasses_requires_artifact(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="dropped")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        r = _try_close(client, op, resolution="dropped")
        assert r.status_code == 200, r.text


def test_success_resolution_still_requires_artifact(tmp_path, monkeypatch):
    """`answered` (success vocab for inquiry) still fires the gate."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="answered")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        r = _try_close(client, op, resolution="answered")
        assert r.status_code == 400
        assert "requires_artifact" in r.json()["detail"]


def test_failed_close_bypasses_requires_artifact(tmp_path, monkeypatch):
    """`failed` is also a non-success terminal — bypass."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="failed")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        # task ops use `failed`; need bind_remote_task=False for the
        # test's manual-close path
        r = client.post("/v2/operations", json={
            "space_id": discord, "kind": "task",
            "title": "p10.4 failed", "opener_actor_handle": "@alice",
            "objective": "x",
            "policy": {
                "requires_artifact": True,
                "bind_remote_task": False,
            },
        })
        assert r.status_code == 201
        op = r.json()["id"]
        r = _try_close(client, op, resolution="failed")
        assert r.status_code == 200, r.text
