"""H3: native v2 task lifecycle endpoints + SDK 메서드."""
from __future__ import annotations

import sys
import uuid

import pytest
from fastapi.testclient import TestClient

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
    from app.behaviors.chat.models import ChatThreadModel
    from app.main import app
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread, suffix="h3"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}


def test_v2_native_task_lifecycle_full_cycle(tmp_path, monkeypatch):
    """SDK 만으로 task open -> claim -> evidence -> approval cycle ->
    complete -> closed. v1_conversation_id 없이 동작."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"])

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)

        # alice opens a task. The /v2/operations default for kind=task
        # is now ``bind_remote_task=False`` (collab-task ops should not
        # block close on a queued RemoteTask). Tests exercising the
        # full executor lifecycle (claim/evidence/approval/complete)
        # opt in explicitly so a RemoteTask row exists to claim.
        r = client.post(
            "/v2/operations",
            json={
                "space_id": discord, "kind": "task",
                "title": "patch X", "objective": "fix the bug",
                "opener_actor_handle": "@alice",
                "policy": {"bind_remote_task": True},
            },
        )
        assert r.status_code == 201, r.text
        op_id = r.json()["id"]

        # claude-pca claims
        r = client.post(
            f"/v2/operations/{op_id}/claim",
            json={"actor_handle": "@claude-pca", "lease_seconds": 300},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["state"] == "claimed"
        lease = body["task"]["current_assignment"]["lease_token"]

        # evidence (auto-flips to executing per H1's wired transition)
        r = client.post(
            f"/v2/operations/{op_id}/evidence",
            json={
                "actor_handle": "@claude-pca", "lease_token": lease,
                "kind": "screenshot", "summary": "patch ready",
                "payload": {},
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["state"] == "executing"

        # approval request -> blocked_approval
        r = client.post(
            f"/v2/operations/{op_id}/approval/request",
            json={
                "actor_handle": "@claude-pca", "lease_token": lease,
                "reason": "deploy to prod", "note": None,
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["state"] == "blocked_approval"

        # H1: approve requires task.approve.destructive cap. alice
        # default doesn't have it -> 403.
        r = client.post(
            f"/v2/operations/{op_id}/approval/resolve",
            json={"actor_handle": "@alice", "resolution": "approved"},
        )
        assert r.status_code == 403
        assert "task.approve.destructive" in r.json()["detail"]

        # Grant the cap and retry.
        # We do this through the live capability service the bridge owns.
        cap = m["app"].state.services
        # access the actual capability_authorizer? The wiring is in
        # main.build_services -- the cap_service instance isn't exposed
        # there. Reach into the ChatConversationService's authorizer.
        # Easier path: use V2Repository + CapabilityService directly,
        # which shares the same DB.
        from app.kernel.v2 import CapabilityService
        cap_service = CapabilityService()
        from app.kernel.v2 import CAP_TASK_APPROVE_DESTRUCTIVE
        with m["db"].session_scope() as s:
            cap_service.grant(
                s, actor_handle="@alice",
                capabilities=[CAP_TASK_APPROVE_DESTRUCTIVE],
            )

        r = client.post(
            f"/v2/operations/{op_id}/approval/resolve",
            json={"actor_handle": "@alice", "resolution": "approved"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "executing"

        # complete
        r = client.post(
            f"/v2/operations/{op_id}/complete",
            json={
                "actor_handle": "@claude-pca", "lease_token": lease,
                "summary": "shipped",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "closed"
        assert body["resolution"] == "completed"


def test_v2_native_task_fail_cycle(tmp_path, monkeypatch):
    """fail path: claim -> fail -> closed/failed."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="fail")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = client.post(
            "/v2/operations",
            json={
                "space_id": discord, "kind": "task",
                "title": "broken", "objective": "do thing",
                "opener_actor_handle": "@alice",
                "policy": {"bind_remote_task": True},
            },
        ).json()["id"]
        r = client.post(
            f"/v2/operations/{op_id}/claim",
            json={"actor_handle": "@claude-pca", "lease_seconds": 300},
        )
        lease = r.json()["task"]["current_assignment"]["lease_token"]
        r = client.post(
            f"/v2/operations/{op_id}/fail",
            json={
                "actor_handle": "@claude-pca", "lease_token": lease,
                "error_text": "broke",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "closed"
        assert body["resolution"] == "failed"


def test_sdk_drives_full_task_cycle(tmp_path, monkeypatch):
    """SDK BridgeV2Client 의 새 메서드들을 in-process 로 호출."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="sdk")
    from app.agent_sdk import BridgeV2Client
    from fastapi.testclient import TestClient as _TC

    def _wire(handle):
        tc = _TC(m["app"], base_url="http://testserver")
        tc.__enter__()
        tc.headers.update({
            "Authorization": "Bearer t",
            "X-Bridge-Client-Id": handle.lstrip("@"),
        })
        c = BridgeV2Client(
            base_url="http://testserver", bearer_token="t", actor_handle=handle,
        )
        c._http.close(); c._http = tc
        c._tc = tc
        return c

    alice = _wire("@alice")
    pca = _wire("@claude-pca")
    try:
        op = alice.open_operation(
            space_id=discord, kind="task", title="patch",
            objective="patch the bug",
            policy={"bind_remote_task": True},
        )
        op_id = op["id"]
        claim_resp = pca.claim_task(op_id, lease_seconds=300)
        lease = claim_resp["task"]["current_assignment"]["lease_token"]
        ev_resp = pca.submit_evidence(
            op_id, lease_token=lease, kind="result",
            summary="ok", payload={},
        )
        assert ev_resp["state"] == "executing"
        complete_resp = pca.complete_task(op_id, lease_token=lease, summary="done")
        assert complete_resp["state"] == "closed"
        assert complete_resp["resolution"] == "completed"
    finally:
        alice._tc.__exit__(None, None, None)
        pca._tc.__exit__(None, None, None)


def test_unknown_actor_lease_token_returns_400(tmp_path, monkeypatch):
    """잘못된 lease_token 으로 evidence 시도 -> 400."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="bad-lease")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = client.post(
            "/v2/operations",
            json={
                "space_id": discord, "kind": "task",
                "title": "x", "objective": "y",
                "opener_actor_handle": "@alice",
                "policy": {"bind_remote_task": True},
            },
        ).json()["id"]
        r = client.post(
            f"/v2/operations/{op_id}/claim",
            json={"actor_handle": "@claude-pca", "lease_seconds": 300},
        )
        # call evidence with WRONG lease
        r = client.post(
            f"/v2/operations/{op_id}/evidence",
            json={
                "actor_handle": "@claude-pca", "lease_token": "FAKE-LEASE",
                "kind": "result", "summary": "x", "payload": {},
            },
        )
        assert r.status_code == 400


def test_non_task_op_cannot_claim(tmp_path, monkeypatch):
    """inquiry 에 claim 시도 -> 400 (kind=task 아님)."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="nontask")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = client.post(
            "/v2/operations",
            json={
                "space_id": discord, "kind": "inquiry",
                "title": "q", "opener_actor_handle": "@alice",
            },
        ).json()["id"]
        r = client.post(
            f"/v2/operations/{op_id}/claim",
            json={"actor_handle": "@claude-pca", "lease_seconds": 300},
        )
        assert r.status_code == 400
