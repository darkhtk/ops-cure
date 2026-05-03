"""T1.2 — speech.evidence carries an artifact descriptor.

Phase 7 makes deliverables first-class: when an agent posts
``speech.evidence`` with ``payload.artifact = {kind, uri, sha256,
mime, size_bytes, label?, metadata?}``, the bridge creates an
``OperationArtifact`` row tied to the evidence event. Future
audits can ask "show me everything produced in this op" via
``GET /v2/operations/{id}/artifacts`` and receive structured
records — not prose-buried path strings.

Pre-T1.2: agents posted prose like "Wrote game/dodge.html in cwd"
and there was no formal connection between the conversation and
the file on disk. The artifact mechanism existed but was wired
only into the v1-style ``RemoteTaskService.add_evidence`` path
(``/v2/operations/{id}/evidence``); the generic
``/v2/operations/{id}/events`` speech.evidence path silently
ignored payload.artifact.
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


def _thread(db, Thread, suffix="t12"):
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
_DUMMY_SHA = "0123456789abcdef" * 4  # 64 hex chars


def _open_inquiry(client, *, discord, title="x"):
    r = client.post(
        "/v2/operations",
        json={
            "space_id": discord, "kind": "inquiry",
            "title": title, "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_speech_evidence_with_artifact_creates_artifact_row(tmp_path, monkeypatch):
    """speech.evidence with valid payload.artifact → artifact row
    visible via GET /artifacts, tied to the evidence event."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="happy")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_inquiry(client, discord=discord)

        r = client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "wrote dodge.html in scratch cwd",
                    "artifact": {
                        "kind": "code",
                        "uri": "file:///scratch/game/dodge.html",
                        "sha256": _DUMMY_SHA,
                        "mime": "text/html",
                        "size_bytes": 5942,
                        "label": "dodge game v1",
                    },
                },
            },
        )
        assert r.status_code == 201, r.text
        evidence_event_id = r.json()["id"]

        r = client.get(f"/v2/operations/{op_id}/artifacts")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["operation_id"] == op_id
        assert len(body["artifacts"]) == 1
        a = body["artifacts"][0]
        assert a["event_id"] == evidence_event_id
        assert a["kind"] == "code"
        assert a["uri"] == "file:///scratch/game/dodge.html"
        assert a["sha256"] == _DUMMY_SHA
        assert a["mime"] == "text/html"
        assert a["size_bytes"] == 5942
        assert a["label"] == "dodge game v1"


def test_speech_evidence_without_artifact_ok(tmp_path, monkeypatch):
    """speech.evidence with no payload.artifact is fine — the field is
    optional. The event is recorded, no artifact row created."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="prose-only")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_inquiry(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {"text": "I claim X happened, no file attached"},
            },
        )
        assert r.status_code == 201, r.text
        r = client.get(f"/v2/operations/{op_id}/artifacts")
        assert r.json()["artifacts"] == []


def test_non_evidence_event_with_artifact_field_is_ignored(tmp_path, monkeypatch):
    """speech.claim / speech.propose / etc. with payload.artifact does
    NOT trigger artifact creation — only speech.evidence is the
    designated carrier. Caller mistake → silent ignore (the prose
    field stays in event payload, no row created)."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="wrong-kind")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_inquiry(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.claim",
                "payload": {
                    "text": "asserting we have dodge.html",
                    "artifact": {
                        "kind": "code", "uri": "file:///x.html",
                        "sha256": _DUMMY_SHA, "mime": "text/html",
                        "size_bytes": 100,
                    },
                },
            },
        )
        assert r.status_code == 201, r.text
        r = client.get(f"/v2/operations/{op_id}/artifacts")
        assert r.json()["artifacts"] == []


def test_evidence_with_partial_artifact_returns_400(tmp_path, monkeypatch):
    """Caller intent is clear (artifact field present with at least
    one required key), but the payload is malformed. The bridge
    rejects with 400 rather than silently dropping the artifact —
    silent drop hides bugs in the executor's stat code."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="malformed")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_inquiry(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "trying to attach but botched the field",
                    "artifact": {
                        "kind": "code",  # missing uri, sha256, etc.
                    },
                },
            },
        )
        assert r.status_code == 400
        assert "missing required fields" in r.json()["detail"]
        # nothing got persisted
        r = client.get(f"/v2/operations/{op_id}/artifacts")
        assert r.json()["artifacts"] == []


def test_evidence_with_bad_sha256_returns_400(tmp_path, monkeypatch):
    """sha256 must be exactly 64 hex chars. Anything else → 400."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="bad-sha")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_inquiry(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "with bad sha",
                    "artifact": {
                        "kind": "code", "uri": "file:///x",
                        "sha256": "deadbeef",  # too short
                        "mime": "text/plain", "size_bytes": 1,
                    },
                },
            },
        )
        assert r.status_code == 400
        assert "sha256" in r.json()["detail"]


def test_multiple_evidence_events_accumulate_artifacts(tmp_path, monkeypatch):
    """Each speech.evidence with an artifact adds another row.
    GET /artifacts returns all of them, ordered by creation."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="multi")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_inquiry(client, discord=discord)

        for i, name in enumerate(["spec.md", "impl.py", "tests.py"]):
            r = client.post(
                f"/v2/operations/{op_id}/events",
                json={
                    "actor_handle": "@operator",
                    "kind": "speech.evidence",
                    "payload": {
                        "text": f"wrote {name}",
                        "artifact": {
                            "kind": "code",
                            "uri": f"file:///scratch/{name}",
                            "sha256": _DUMMY_SHA[:-2] + f"{i:02d}",
                            "mime": "text/plain",
                            "size_bytes": 100 + i,
                        },
                    },
                },
            )
            assert r.status_code == 201, r.text

        r = client.get(f"/v2/operations/{op_id}/artifacts")
        body = r.json()
        assert len(body["artifacts"]) == 3
        uris = [a["uri"] for a in body["artifacts"]]
        assert all(u.startswith("file:///scratch/") for u in uris)


def test_artifact_filter_by_kind(tmp_path, monkeypatch):
    """GET /artifacts?kind=screenshot filters server-side."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="filter")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_inquiry(client, discord=discord)

        for kind, mime in [("code", "text/x-python"), ("screenshot", "image/png")]:
            r = client.post(
                f"/v2/operations/{op_id}/events",
                json={
                    "actor_handle": "@operator",
                    "kind": "speech.evidence",
                    "payload": {
                        "text": f"posting {kind}",
                        "artifact": {
                            "kind": kind, "uri": f"file:///x.{kind}",
                            "sha256": _DUMMY_SHA, "mime": mime,
                            "size_bytes": 1,
                        },
                    },
                },
            )
            assert r.status_code == 201

        r = client.get(f"/v2/operations/{op_id}/artifacts?kind=screenshot")
        body = r.json()
        assert len(body["artifacts"]) == 1
        assert body["artifacts"][0]["kind"] == "screenshot"
