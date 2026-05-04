"""P12-2: V2Repository.recent_active_ops + last_event_for_op."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

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
    from app.kernel.v2 import V2Repository
    from app.kernel.v2.models import OperationV2Model, OperationEventV2Model
    from app.kernel.v2.actor_service import ActorService
    db.init_db()
    return locals()


def _new_op(db_mod, repo, actors, kind="task", state="open", **kw):
    from app.kernel.v2.models import OperationV2Model
    with db_mod.session_scope() as s:
        actors.ensure_actor_by_handle(s, handle="@alice")
        op = OperationV2Model(
            id=str(uuid.uuid4()),
            space_id=kw.get("space_id", "test-space"),
            kind=kind,
            title=kw.get("title", "test op"),
            state=state,
        )
        s.add(op); s.flush()
        op_id = op.id
        return op_id


def _post_event(db_mod, repo, op_id, kind="chat.speech.claim", actor_handle="@alice"):
    from app.kernel.v2.actor_service import ActorService
    with db_mod.session_scope() as s:
        actor = ActorService(repo).ensure_actor_by_handle(s, handle=actor_handle)
        ev = repo.insert_event(
            s,
            operation_id=op_id,
            actor_id=actor.id,
            kind=kind,
            payload={"text": "hi"},
        )
        s.flush()
        return ev.id, ev.seq


def test_recent_active_ops_returns_open_only(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    open_id = _new_op(m["db"], repo, actors, state="open", title="o")
    closed_id = _new_op(m["db"], repo, actors, state="closed", title="c")

    with m["db"].session_scope() as s:
        ids = [op.id for op in repo.recent_active_ops(s)]
    assert open_id in ids
    assert closed_id not in ids


def test_recent_active_ops_since_filter(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    op_id = _new_op(m["db"], repo, actors, state="open")

    far_future = datetime.now(timezone.utc) + timedelta(hours=1)
    with m["db"].session_scope() as s:
        ids = [op.id for op in repo.recent_active_ops(s, since=far_future)]
    assert ids == [], "since filter should exclude ops older than the cutoff"


def test_recent_active_ops_includes_executing_and_claimed(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    op_open = _new_op(m["db"], repo, actors, state="open", title="o")
    op_claimed = _new_op(m["db"], repo, actors, state="claimed", title="cl")
    op_exec = _new_op(m["db"], repo, actors, state="executing", title="ex")
    op_blocked = _new_op(m["db"], repo, actors, state="blocked_approval", title="bl")
    op_verify = _new_op(m["db"], repo, actors, state="verifying", title="ve")

    with m["db"].session_scope() as s:
        ids = {op.id for op in repo.recent_active_ops(s)}
    assert {op_open, op_claimed, op_exec, op_blocked, op_verify}.issubset(ids)


def test_last_event_for_op_returns_highest_seq(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    op_id = _new_op(m["db"], repo, actors)
    _, seq1 = _post_event(m["db"], repo, op_id, kind="chat.speech.claim")
    _, seq2 = _post_event(m["db"], repo, op_id, kind="chat.speech.propose")
    _, seq3 = _post_event(m["db"], repo, op_id, kind="chat.speech.ratify")

    with m["db"].session_scope() as s:
        last = repo.last_event_for_op(s, operation_id=op_id)
        assert last is not None
        assert last.seq == seq3


def test_last_event_for_op_empty_op_returns_none(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    op_id = _new_op(m["db"], repo, actors)
    with m["db"].session_scope() as s:
        last = repo.last_event_for_op(s, operation_id=op_id)
        assert last is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
