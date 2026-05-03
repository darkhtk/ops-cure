"""P9.1 / D9 — ratify intent split.

`chat.speech.ratify` is overloaded: agents use it both to agree
with a spec proposal AND to vote for closing the op. The quorum
gate originally counted all ratify events indiscriminately, so a
spec consensus on early proposals tripped premature close attempts
that got blocked by ``requires_artifact``.

Rev 9 fix: only ratifies that carry CLOSE-INTENT count toward
quorum. A ratify is close-intent when ANY of:

  1. ``payload.intent == "close"`` (explicit, recommended)
  2. ``replies_to_event_id`` → ``chat.speech.move_close``
  3. ``replies_to_event_id`` → event with attached OperationArtifact
  4. The op already has ≥1 artifact attached at or before the
     ratify's seq (back-compat heuristic)

Spec ratifies (without any of these) are recorded but not counted.
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


def _thread(db, Thread, suffix="d9"):
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


def _open(client, *, discord, **policy):
    r = client.post("/v2/operations", json={
        "space_id": discord, "kind": "inquiry",
        "title": "ratify-intent probe", "opener_actor_handle": "@alice",
        "policy": {
            "close_policy": "quorum", "min_ratifiers": 2, **policy,
        },
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post(client, op, body):
    r = client.post(f"/v2/operations/{op}/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _try_close(client, op):
    return client.post(
        f"/v2/operations/{op}/close",
        json={
            "actor_handle": "@alice",
            "resolution": "answered",
            "summary": "test",
        },
    )


def test_spec_ratifies_alone_do_not_satisfy_quorum(tmp_path, monkeypatch):
    """RPG/Unity-arcade pattern: 3 personas ratify the designer's
    spec proposal at the start. Without close-intent signals the
    quorum is NOT met and the op stays open."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="spec-only")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        propose_id = _post(client, op, {
            "actor_handle": "@designer",
            "kind": "speech.propose",
            "payload": {"text": "spec v1"},
        })
        # Three personas ratify the SPEC (replies_to=spec propose,
        # which has no artifact)
        for h in ("@reviewer", "@operator", "@investigator"):
            _post(client, op, {
                "actor_handle": h, "kind": "speech.ratify",
                "payload": {"text": f"[RATIFY] {h}: spec looks good"},
                "replies_to_event_id": propose_id,
            })
        r = _try_close(client, op)
        assert r.status_code == 400
        assert "quorum" in r.json()["detail"]


def test_explicit_intent_close_counts(tmp_path, monkeypatch):
    """payload.intent='close' on a ratify is the explicit
    close-intent signal — counts even with no replies_to."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="explicit")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        for h in ("@reviewer", "@operator"):
            _post(client, op, {
                "actor_handle": h, "kind": "speech.ratify",
                "payload": {"text": f"[RATIFY] {h}", "intent": "close"},
            })
        r = _try_close(client, op)
        assert r.status_code == 200, r.text


def test_ratify_replying_to_move_close_counts(tmp_path, monkeypatch):
    """A ratify replying to a chat.speech.move_close trigger is
    obviously close-intent — counts toward quorum."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="move-close")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        mc_id = _post(client, op, {
            "actor_handle": "@alice", "kind": "speech.move_close",
            "payload": {"text": "let's wrap"},
        })
        for h in ("@reviewer", "@operator"):
            _post(client, op, {
                "actor_handle": h, "kind": "speech.ratify",
                "payload": {"text": f"[RATIFY] {h}"},
                "replies_to_event_id": mc_id,
            })
        r = _try_close(client, op)
        assert r.status_code == 200, r.text


def test_ratify_replying_to_evidence_with_artifact_counts(tmp_path, monkeypatch):
    """A ratify replying to a speech.evidence that has an artifact
    counts — the ratifier is endorsing the deliverable."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="evidence-vote")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        ev_id = _post(client, op, {
            "actor_handle": "@operator", "kind": "speech.evidence",
            "payload": {
                "text": "shipped",
                "artifact": {
                    "kind": "code", "uri": "file:///x.py",
                    "sha256": _DUMMY_SHA, "mime": "text/x-python",
                    "size_bytes": 100,
                },
            },
        })
        for h in ("@reviewer", "@designer"):
            _post(client, op, {
                "actor_handle": h, "kind": "speech.ratify",
                "payload": {"text": f"[RATIFY] {h}"},
                "replies_to_event_id": ev_id,
            })
        r = _try_close(client, op)
        assert r.status_code == 200, r.text


def test_ratify_after_artifact_attached_counts_via_heuristic(tmp_path, monkeypatch):
    """Back-compat heuristic: ratify with no replies_to and no
    explicit intent is treated as close-intent if the op already
    has ≥1 artifact attached at-or-before the ratify's seq.
    Catches the most common case without requiring callers to
    update their flow."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="heuristic")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        # Artifact attached
        _post(client, op, {
            "actor_handle": "@operator", "kind": "speech.evidence",
            "payload": {
                "text": "shipped",
                "artifact": {
                    "kind": "code", "uri": "file:///y.py",
                    "sha256": _DUMMY_SHA, "mime": "text/x-python",
                    "size_bytes": 50,
                },
            },
        })
        # Bare ratifies after the artifact arrives
        for h in ("@reviewer", "@designer"):
            _post(client, op, {
                "actor_handle": h, "kind": "speech.ratify",
                "payload": {"text": f"[RATIFY] {h}"},
            })
        r = _try_close(client, op)
        assert r.status_code == 200, r.text


def test_ratify_before_any_artifact_does_not_count_via_heuristic(tmp_path, monkeypatch):
    """The heuristic specifically requires the artifact to exist at
    or before the ratify's seq. A ratify posted before any
    artifact existed must NOT be retroactively credited when an
    artifact arrives later."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="seq-order")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        # Two bare ratifies BEFORE any artifact (the spec-ack pattern)
        for h in ("@reviewer", "@designer"):
            _post(client, op, {
                "actor_handle": h, "kind": "speech.ratify",
                "payload": {"text": f"[RATIFY] {h}: like the plan"},
            })
        # Then artifact arrives
        _post(client, op, {
            "actor_handle": "@operator", "kind": "speech.evidence",
            "payload": {
                "text": "shipped",
                "artifact": {
                    "kind": "code", "uri": "file:///z.py",
                    "sha256": _DUMMY_SHA, "mime": "text/x-python",
                    "size_bytes": 50,
                },
            },
        })
        r = _try_close(client, op)
        assert r.status_code == 400
        assert "quorum" in r.json()["detail"]


def test_distinct_actor_dedup_still_holds(tmp_path, monkeypatch):
    """Same actor double-ratifying with intent=close still counts
    as one. The dedup logic predates D9 and is preserved."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="dedup")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        for _ in range(3):
            _post(client, op, {
                "actor_handle": "@reviewer", "kind": "speech.ratify",
                "payload": {"text": "[RATIFY] reviewer", "intent": "close"},
            })
        r = _try_close(client, op)
        assert r.status_code == 400
        assert "quorum" in r.json()["detail"]
