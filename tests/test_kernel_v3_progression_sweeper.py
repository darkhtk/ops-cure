"""P12-4: ProgressionSweeper decision matrix.

These tests pin every failure mode enumerated in the phase-12 plan:
- truly TERMINAL → skip
- closed/abandoned op → not visible to recent_active_ops
- self-loop → skip
- already replied → skip
- max_retries → defer
- expected_response only → nudge
- addressed_to only → nudge
- replies_to author → nudge
- not yet idle → skip
- stale/unknown actor → skip
"""
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
    from app.kernel.v2.models import OperationV2Model, OperationEventV2Model, ActorV2Model
    from app.kernel.v2.actor_service import ActorService
    from app.kernel.v2.progression_sweeper import ProgressionSweeper, SweepAction
    db.init_db()
    return locals()


def _new_op(m, state="open"):
    from app.kernel.v2.models import OperationV2Model
    with m["db"].session_scope() as s:
        op = OperationV2Model(
            id=str(uuid.uuid4()),
            space_id="t-space",
            kind="task",
            title="t",
            state=state,
        )
        s.add(op); s.flush()
        return op.id


def _ensure_actor(m, handle):
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    with m["db"].session_scope() as s:
        a = actors.ensure_actor_by_handle(s, handle=handle)
        return a.id


def _post(m, op_id, *, actor_id, kind, payload=None,
          addressed_actor_ids=None, replies_to=None,
          created_at_offset_s=0):
    """Insert a raw event with full control over fields the public
    API doesn't normally expose (created_at, kind override)."""
    from app.kernel.v2.models import OperationEventV2Model
    from sqlalchemy import select, func
    import json
    with m["db"].session_scope() as s:
        max_seq = s.scalar(
            select(func.coalesce(func.max(OperationEventV2Model.seq), 0))
            .where(OperationEventV2Model.operation_id == op_id)
        ) or 0
        ev = OperationEventV2Model(
            operation_id=op_id,
            actor_id=actor_id,
            seq=int(max_seq) + 1,
            kind=kind,
            payload_json=json.dumps(payload or {}),
            addressed_to_actor_ids_json=json.dumps(addressed_actor_ids or []),
            replies_to_event_id=replies_to,
        )
        if created_at_offset_s:
            ev.created_at = (
                datetime.now(timezone.utc) + timedelta(seconds=created_at_offset_s)
            )
        s.add(ev); s.flush()
        ev_id = ev.id
        ev_seq = ev.seq
        return ev_id, ev_seq


# ---------------------------------------------------------------------------


def test_truly_terminal_event_skipped(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        created_at_offset_s=-3600,  # an hour ago — definitely idle
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    assert len(actions) == 1
    assert actions[0].op_id == op_id
    assert actions[0].action == "skip"
    assert "terminal" in actions[0].reason


def test_closed_op_not_swept(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m, state="closed")
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    assert all(a.op_id != op_id for a in actions), "closed op must not appear"


def test_not_idle_yet_skipped(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    bob = _ensure_actor(m, "@bob")
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        addressed_actor_ids=[bob],
        # no offset — created just now
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "skip"
    assert "not idle" in a.reason


def test_addressed_to_triggers_nudge(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    bob = _ensure_actor(m, "@bob")
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        addressed_actor_ids=[bob],
        created_at_offset_s=-3600,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "nudge"
    assert a.target_actor_id == bob
    assert a.target_handle == "@bob"


def test_expected_response_triggers_nudge(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    _ensure_actor(m, "@curator")  # exists as a row so handle resolves
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        payload={"text": "hi", "_meta": {
            "expected_response": {"from_actor_handles": ["@curator"]}
        }},
        created_at_offset_s=-3600,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "nudge"
    assert a.target_handle == "@curator"


def test_replies_to_author_triggers_nudge(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    bob = _ensure_actor(m, "@bob")
    # Bob speaks first
    bob_ev_id, _ = _post(
        m, op_id, actor_id=bob, kind="chat.speech.propose",
        created_at_offset_s=-7200,
    )
    # Alice replies to Bob with no expected_response, no addressed_to
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.object",
        replies_to=bob_ev_id,
        created_at_offset_s=-3600,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "nudge"
    assert a.target_actor_id == bob, "replies_to author should be nudged"


def test_self_loop_skipped(tmp_path, monkeypatch):
    """Last speaker addressing themselves shouldn't trigger a nudge."""
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        addressed_actor_ids=[alice],
        created_at_offset_s=-3600,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "skip"
    assert "self-loop" in a.reason


def test_terminal_reply_after_request_skipped(tmp_path, monkeypatch):
    """When the last speech event IS the reply (TERMINAL, no further
    next-responder signal), the sweeper takes "terminal" — that's the
    natural way "already replied" surfaces (the reply itself becomes
    the last speech event, and replies are TERMINAL by default)."""
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    bob = _ensure_actor(m, "@bob")
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        addressed_actor_ids=[bob],
        created_at_offset_s=-3600,
    )
    # bob already replied (TERMINAL — no addressed_to, no expected_response)
    _post(
        m, op_id, actor_id=bob, kind="chat.speech.agree",
        created_at_offset_s=-1800,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "skip"
    assert "terminal" in a.reason


def test_stale_handle_skipped(tmp_path, monkeypatch):
    """expected_response references a handle with no actor row — no-op."""
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        payload={"_meta": {
            "expected_response": {"from_actor_handles": ["@ghost"]}
        }},
        created_at_offset_s=-3600,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "skip"
    assert "stale" in a.reason or "unknown" in a.reason


def test_max_retries_then_defer(tmp_path, monkeypatch):
    """Two prior nudges on the same trigger → 3rd tick escalates to defer."""
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    bob = _ensure_actor(m, "@bob")
    system = _ensure_actor(m, "@system")
    trigger_id, _ = _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        addressed_actor_ids=[bob],
        created_at_offset_s=-3600,
    )
    # Two prior nudges (system to bob, replying-to trigger)
    _post(
        m, op_id, actor_id=system, kind="chat.system.nudge",
        addressed_actor_ids=[bob], replies_to=trigger_id,
        created_at_offset_s=-1800,
    )
    _post(
        m, op_id, actor_id=system, kind="chat.system.nudge",
        addressed_actor_ids=[bob], replies_to=trigger_id,
        created_at_offset_s=-900,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    # System events don't change the "last speech event" — the trigger
    # claim is still the last speech message. The sweeper sees 2 prior
    # nudges and escalates.
    assert a.action == "defer"
    assert a.target_actor_id == bob
    assert a.replies_to_event_id == trigger_id


def test_one_prior_nudge_still_nudges(tmp_path, monkeypatch):
    """One system.nudge already exists trailing the trigger; the
    sweeper sees the last SPEECH event as the trigger (system events
    don't shadow it) and emits a second nudge (count=1 < max=2)."""
    m = _bootstrap(tmp_path, monkeypatch)
    op_id = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    bob = _ensure_actor(m, "@bob")
    system = _ensure_actor(m, "@system")
    trigger_id, _ = _post(
        m, op_id, actor_id=alice, kind="chat.speech.claim",
        addressed_actor_ids=[bob],
        created_at_offset_s=-3600,
    )
    _post(
        m, op_id, actor_id=system, kind="chat.system.nudge",
        addressed_actor_ids=[bob], replies_to=trigger_id,
        created_at_offset_s=-1800,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    a = next(a for a in actions if a.op_id == op_id)
    assert a.action == "nudge", f"expected nudge, got {a.action}: {a.reason}"
    assert a.target_actor_id == bob


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
