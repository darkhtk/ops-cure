"""D1+D5 — universal carve-outs (`defer`, `evidence`, `object`) for
the reply-kind whitelist gate.

RPG smoke (2026-05-04) discovered a deadlock: when @reviewer
posts ``[OBJECT kinds=agree,object]`` demanding a patch, the
@operator's reply with ``[EVIDENCE]`` (the patched file) was
rejected ``policy.reply_kind_rejected``. Six rejected posts later
the op was stuck — agent_loop silently dropped each 400 and the
whole operation needed manual intervention to continue.

Root cause: `kinds=` was originally designed to shape *voting*
moments — but the policy gate applied it uniformly, including to
demand-patch loops where the responder MUST be able to attach a
patched deliverable.

Fix (rev 8): three universal carve-outs admissible regardless of
the trigger's whitelist:

  - `defer`    → required by the auto-defer sweeper
  - `evidence` → deliverable carrier (T1.2)
  - `object`   → late counter-evidence always valid
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


def _thread(db, Thread, suffix="d1"):
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


def _open(client, *, discord, kind="inquiry"):
    r = client.post("/v2/operations", json={
        "space_id": discord, "kind": kind,
        "title": "carve-out probe", "opener_actor_handle": "@alice",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post_trigger_with_narrow_kinds(client, op, *, kinds):
    """Reviewer posts an OBJECT that narrows the whitelist."""
    r = client.post(
        f"/v2/operations/{op}/events",
        json={
            "actor_handle": "@reviewer",
            "kind": "speech.object",
            "payload": {"text": "your code is wrong, fix it"},
            "expected_response": {
                "from_actor_handles": ["@operator"],
                "kinds": kinds,
            },
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_evidence_admitted_despite_narrow_whitelist(tmp_path, monkeypatch):
    """[OBJECT kinds=agree,object] from reviewer ⇒ operator can still
    post [EVIDENCE] with the patched file. The deadlock from RPG
    smoke is now broken automatically."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="evid-carveout")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        trigger_id = _post_trigger_with_narrow_kinds(
            client, op, kinds=["agree", "object"],
        )
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "patched, please re-review",
                    "artifact": {
                        "kind": "code", "uri": "file:///x.py",
                        "sha256": _DUMMY_SHA, "mime": "text/x-python",
                        "size_bytes": 100,
                    },
                },
                "replies_to_event_id": trigger_id,
            },
        )
        assert r.status_code == 201, r.text


def test_object_admitted_despite_narrow_whitelist(tmp_path, monkeypatch):
    """A counter-OBJECT to a narrow [PROPOSE kinds=ratify] is always
    admissible — disagreement must never be silenced by a too-tight
    whitelist."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="obj-carveout")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        # opener proposes with a narrow whitelist
        trigger = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@alice",
                "kind": "speech.propose",
                "payload": {"text": "ship it"},
                "expected_response": {
                    "from_actor_handles": ["@reviewer"],
                    "kinds": ["ratify"],  # only ratify allowed in spirit
                },
            },
        )
        assert trigger.status_code == 201
        trigger_id = trigger.json()["id"]
        # reviewer disagrees — [OBJECT] must still go through
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@reviewer",
                "kind": "speech.object",
                "payload": {"text": "no, this regresses production"},
                "replies_to_event_id": trigger_id,
            },
        )
        assert r.status_code == 201, r.text


def test_defer_admitted_despite_narrow_whitelist(tmp_path, monkeypatch):
    """The pre-existing defer carve-out still works (regression
    guard for the auto-defer sweeper)."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="defer-carveout")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        trigger_id = _post_trigger_with_narrow_kinds(
            client, op, kinds=["agree"],
        )
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.defer",
                "payload": {"text": "I cannot answer in agree-form"},
                "replies_to_event_id": trigger_id,
            },
        )
        assert r.status_code == 201, r.text


def test_non_carveout_still_rejected_when_outside_whitelist(tmp_path, monkeypatch):
    """Carve-outs are intentional, not blanket. Non-carve-out kinds
    (claim, ratify, agree, propose, etc.) still respect the trigger's
    whitelist — otherwise the gate would be useless."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="non-carveout")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        trigger_id = _post_trigger_with_narrow_kinds(
            client, op, kinds=["agree", "object"],
        )
        # claim is NOT a carve-out, NOT in whitelist → 400
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.claim",
                "payload": {"text": "I think we should..."},
                "replies_to_event_id": trigger_id,
            },
        )
        assert r.status_code == 400
        assert "reply_kind_rejected" in r.json()["detail"] or "claim" in r.json()["detail"]


def test_wildcard_still_accepts_any_kind(tmp_path, monkeypatch):
    """The `*` value remains the explicit "any kind" sentinel."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="wildcard")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        trigger_id = _post_trigger_with_narrow_kinds(
            client, op, kinds=["*"],
        )
        for k in ("speech.claim", "speech.propose", "speech.react", "speech.agree"):
            r = client.post(
                f"/v2/operations/{op}/events",
                json={
                    "actor_handle": "@operator",
                    "kind": k,
                    "payload": {"text": f"posting as {k}"},
                    "replies_to_event_id": trigger_id,
                },
            )
            assert r.status_code == 201, (k, r.text)
