"""T2.1 — ``policy.requires_artifact`` close gate.

When True, close is rejected (HTTP 400, code
``policy.close_needs_artifact``) until at least one
``OperationArtifact`` is attached to the op. The artifact arrives
through ``speech.evidence`` carrying a ``payload.artifact`` (T1.2).

This is orthogonal to ``close_policy`` — even when quorum or
operator-ratify is satisfied, the close still fails without a
deliverable. Default ``requires_artifact=false`` keeps existing
ops behaving exactly as before.
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


def _thread(db, Thread, suffix="t21"):
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


def _open(client, *, discord, policy, kind="inquiry", title="x"):
    r = client.post(
        "/v2/operations",
        json={
            "space_id": discord, "kind": kind,
            "title": title, "opener_actor_handle": "@alice",
            "policy": policy,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post_evidence_with_artifact(client, op_id, *, name, sha=_DUMMY_SHA):
    return client.post(
        f"/v2/operations/{op_id}/events",
        json={
            "actor_handle": "@operator",
            "kind": "speech.evidence",
            "payload": {
                "text": f"wrote {name}",
                "artifact": {
                    "kind": "code", "uri": f"file:///{name}",
                    "sha256": sha, "mime": "text/plain",
                    "size_bytes": 100,
                },
            },
        },
    )


def test_close_blocked_when_requires_artifact_and_none_attached(tmp_path, monkeypatch):
    """alice opens an op with requires_artifact=true. No evidence is
    posted. Close is rejected with policy.close_needs_artifact."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="block-no-art")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open(
            client, discord=discord,
            policy={"requires_artifact": True},
        )
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "answered",
                "summary": "no deliverable",
            },
        )
        assert r.status_code == 400, r.text
        assert "requires_artifact" in r.json()["detail"]


def test_close_admitted_after_artifact_attached(tmp_path, monkeypatch):
    """Same op but operator posts speech.evidence with artifact first.
    Close now succeeds."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="ok-after-art")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open(
            client, discord=discord,
            policy={"requires_artifact": True},
        )
        r = _post_evidence_with_artifact(client, op_id, name="result.json")
        assert r.status_code == 201, r.text
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "answered",
                "summary": "deliverable attached",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "closed"


def test_default_does_not_require_artifact(tmp_path, monkeypatch):
    """Without explicit requires_artifact, close behavior is unchanged."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="default")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open(client, discord=discord, policy={})
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "answered",
                "summary": "no artifact, no requirement",
            },
        )
        assert r.status_code == 200, r.text


def test_combined_with_quorum_close_policy(tmp_path, monkeypatch):
    """requires_artifact + close_policy=quorum: BOTH must be
    satisfied. The error code surfaces whichever check fails first.
    Per implementation, requires_artifact is checked before the
    close_policy switch — so a no-artifact op fails with
    policy.close_needs_artifact even if quorum is also missing."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="combined")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open(
            client, discord=discord,
            policy={
                "close_policy": "quorum",
                "min_ratifiers": 2,
                "requires_artifact": True,
            },
        )
        # No artifact, no ratifiers — close blocked.
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "answered", "summary": "?",
            },
        )
        assert r.status_code == 400
        assert "requires_artifact" in r.json()["detail"]

        # Attach artifact — still blocked, now on quorum.
        r = _post_evidence_with_artifact(client, op_id, name="x.txt")
        assert r.status_code == 201
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "answered", "summary": "?",
            },
        )
        assert r.status_code == 400
        # Now quorum is the missing piece.
        assert "quorum" in r.json()["detail"] or "ratif" in r.json()["detail"]

        # 2 distinct ratifiers — close succeeds.
        for handle in ("@reviewer", "@operator"):
            client.post(
                f"/v2/operations/{op_id}/events",
                json={
                    "actor_handle": handle, "kind": "speech.ratify",
                    "payload": {"text": f"[RATIFY] {handle}"},
                },
            )
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "answered", "summary": "all gates passed",
            },
        )
        assert r.status_code == 200


def test_invalid_policy_value_returns_400_at_open(tmp_path, monkeypatch):
    """policy.requires_artifact must be a bool. String 'true' is invalid."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="bad-policy")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        r = client.post(
            "/v2/operations",
            json={
                "space_id": discord, "kind": "inquiry",
                "title": "x", "opener_actor_handle": "@alice",
                "policy": {"requires_artifact": "true"},  # str, not bool
            },
        )
        assert r.status_code == 400
        assert "requires_artifact" in r.json()["detail"]
