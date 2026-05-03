"""T1.1 — ``policy.bind_remote_task`` controls whether kind=task ops
bind a RemoteTask row.

Background: pre-T1.1, every kind=task conversation created a
``RemoteTaskModel`` and pinned its id to ``bound_task_id``. The close
path then refused manual close while the task was still
``queued/executing``. For collab-only task ops (where personas just
collaborate on a deliverable, no executor will ever ``claim`` the
task) this turned successful quorum-ratification into a hung op —
ratifiers=2, but close blocked by a v1-era guard about an orphan
RemoteTask that no one ever intended to claim.

T1.1 fix: a new ``policy.bind_remote_task`` boolean. The v1 chat
path keeps ``True`` by default (back-compat). The v3
``/v2/operations`` path defaults to ``False`` for kind=task — collab
task ops now close cleanly under quorum, and ops that *do* want the
full executor lifecycle (claim/evidence/approval/complete) opt in
with ``policy: {"bind_remote_task": true}``.
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


def _thread(db, Thread, suffix="t11"):
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


def _open_op(client, *, discord, kind, policy=None, title="x", **extra):
    body = {
        "space_id": discord,
        "kind": kind,
        "title": title,
        "opener_actor_handle": "@alice",
        **extra,
    }
    if policy is not None:
        body["policy"] = policy
    r = client.post("/v2/operations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_v3_kind_task_default_does_not_bind_remote_task(tmp_path, monkeypatch):
    """Default for /v2/operations + kind=task is bind_remote_task=False."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="default-no-bind")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_op(
            client, discord=discord, kind="task",
            objective="collab-only deliverable",
        )
        # Resolve underlying v1 conversation row to inspect bound_task_id.
        from app.behaviors.chat.models import ChatConversationModel
        from app.kernel.v2 import V2Repository
        repo = V2Repository()
        with m["db"].session_scope() as s:
            op = repo.get_operation(s, op_id)
            v1_id = repo.operation_metadata(op).get("v1_conversation_id")
            assert v1_id, "v2 op must have v1 mirror"
            v1 = s.get(ChatConversationModel, v1_id)
            assert v1 is not None
            assert v1.bound_task_id is None, (
                "default kind=task on /v2/operations must NOT bind a RemoteTask"
            )


def test_v3_kind_task_explicit_bind_true_creates_remote_task(tmp_path, monkeypatch):
    """policy.bind_remote_task=True restores the legacy executor binding."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="bind-true")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op_id = _open_op(
            client, discord=discord, kind="task",
            objective="real executor work",
            policy={"bind_remote_task": True},
        )
        from app.behaviors.chat.models import ChatConversationModel
        from app.kernel.v2 import V2Repository
        repo = V2Repository()
        with m["db"].session_scope() as s:
            op = repo.get_operation(s, op_id)
            v1_id = repo.operation_metadata(op).get("v1_conversation_id")
            v1 = s.get(ChatConversationModel, v1_id)
            assert v1.bound_task_id is not None, (
                "policy.bind_remote_task=True must create a bound RemoteTask"
            )


def test_v3_collab_task_op_closes_cleanly_under_quorum(tmp_path, monkeypatch):
    """End-to-end: opening a collab-task op (no bind) and ratifying it
    under quorum closes the op without hitting the v1 task-bound
    guard. This is the bug T1.1 was filed to fix — pre-T1.1 this
    would error with ``task-bound conversation cannot be manually
    closed while task is queued``."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="quorum-close")

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)

        op_id = _open_op(
            client,
            discord=discord,
            kind="task",
            objective="build dodge.html",
            policy={"close_policy": "quorum", "min_ratifiers": 2},
        )

        # Two distinct ratifiers post chat.speech.ratify.
        # D9 (rev 9): ratifies count toward quorum only when they
        # carry close-intent. Easiest signal: ``payload.intent='close'``.
        for handle in ("@reviewer", "@operator"):
            r = client.post(
                f"/v2/operations/{op_id}/events",
                json={
                    "actor_handle": handle,
                    "kind": "speech.ratify",
                    "payload": {
                        "text": f"[RATIFY] {handle} approves.",
                        "intent": "close",
                    },
                },
            )
            assert r.status_code == 201, r.text

        # alice closes — must succeed (no v1 task guard, quorum met).
        r = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "completed",
                "summary": "quorum-ratified",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "closed"
        assert body["resolution"] == "completed"


def test_v1_chat_path_still_binds_remote_task_by_default(tmp_path, monkeypatch):
    """v1 chat callers (no policy) preserve the legacy default of
    binding a RemoteTask. This guards back-compat — v1 chat tests
    and existing executor flows depend on this."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="v1-back-compat")

    # Drive the chat service directly the way v1 callers do (no
    # /v2/operations layer in front, so no v3 default flip). We need
    # the lifespan wiring to attach state.services, so wrap the call
    # in a TestClient context even though we don't make HTTP calls.
    from app.behaviors.chat.conversation_schemas import ConversationOpenRequest
    from app.behaviors.chat.models import ChatConversationModel

    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        services = m["app"].state.services
        chat_service = services.chat_conversation_service
        chat_service.ensure_general(discord_thread_id=discord)
        summary = chat_service.open_conversation(
            discord_thread_id=discord,
            request=ConversationOpenRequest(
                kind="task",
                title="legacy",
                opener_actor="@alice",
                objective="legacy executor work",
            ),
        )
        with m["db"].session_scope() as s:
            v1 = s.get(ChatConversationModel, summary.id)
            assert v1.bound_task_id is not None, (
                "v1 chat path must keep bind_remote_task=True default"
            )
