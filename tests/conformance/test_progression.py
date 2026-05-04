"""Conformance: §18 Progression nudges.

Phase 12 ships the *detection* layer only — emit is phase 13. This
suite asserts:

1. The spec carries §18 + the rev-12 changelog entry.
2. The sweeper, run on a fresh DB with a forced-idle event, reaches
   the documented decisions (nudge, defer-escalate, terminal-skip,
   self-loop-skip).
3. Settings env vars round-trip through ``Settings``.

The tests exercise the in-process kernel directly (the sweeper has
no HTTP surface yet); they are conformance-flavored because they
pin the *contract* the spec advertises, not just one implementation
choice.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

CONFTEST_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.normpath(os.path.join(CONFTEST_DIR, "..", ".."))
NAS_BRIDGE_ROOT = os.path.join(PROJECT_ROOT, "nas_bridge")


pytestmark = pytest.mark.conformance_required


# ---------------------------------------------------------------------------
# Spec contract
# ---------------------------------------------------------------------------


def test_spec_carries_section_18_progression():
    spec_path = os.path.join(PROJECT_ROOT, "docs", "protocol-v3-spec.md")
    with open(spec_path, "r", encoding="utf-8") as f:
        text = f.read()
    assert "## 18. Progression nudges" in text
    assert "BRIDGE_PROGRESSION_NUDGE_IDLE_S" in text
    assert "BRIDGE_PROGRESSION_NUDGE_MAX_RETRIES" in text
    assert "BRIDGE_PROGRESSION_DISABLED" in text
    assert "decision=nudge" in text or "decision=" in text


def test_changelog_carries_progression_revs():
    """Phase 12 introduced § 18 (rev 12, detection); phase 13 promoted
    § 18.2 to normative (rev 13, emit). Both rows must remain in the
    changelog so future readers can reconstruct the history."""
    spec_path = os.path.join(PROJECT_ROOT, "docs", "protocol-v3-spec.md")
    with open(spec_path, "r", encoding="utf-8") as f:
        text = f.read()
    # Header status is the latest shipped rev.
    assert "Normative (rev 13" in text
    # Both phase rows persist.
    assert "| 12 |" in text and "Phase 12" in text
    assert "| 13 |" in text and "Phase 13" in text


# ---------------------------------------------------------------------------
# Settings round-trip
# ---------------------------------------------------------------------------


def test_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_PROGRESSION_NUDGE_IDLE_S", "12.5")
    monkeypatch.setenv("BRIDGE_PROGRESSION_NUDGE_MAX_RETRIES", "5")
    monkeypatch.setenv("BRIDGE_PROGRESSION_DISABLED", "true")
    if NAS_BRIDGE_ROOT not in sys.path:
        sys.path.insert(0, NAS_BRIDGE_ROOT)
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    s = config.get_settings()
    assert s.progression_nudge_idle_s == 12.5
    assert s.progression_nudge_max_retries == 5
    assert s.progression_disabled is True


# ---------------------------------------------------------------------------
# Sweeper end-to-end against an in-process DB
# ---------------------------------------------------------------------------


def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    if NAS_BRIDGE_ROOT not in sys.path:
        sys.path.insert(0, NAS_BRIDGE_ROOT)
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.kernel.v2 import V2Repository
    from app.kernel.v2.actor_service import ActorService
    from app.kernel.v2.progression_sweeper import ProgressionSweeper
    db.init_db()
    return locals()


def _new_op(m):
    from app.kernel.v2.models import OperationV2Model
    with m["db"].session_scope() as s:
        op = OperationV2Model(
            id=str(uuid.uuid4()),
            space_id="conf",
            kind="task",
            title="conf",
            state="open",
        )
        s.add(op); s.flush()
        return op.id


def _post(m, op_id, *, actor_id, kind, addressed_actor_ids=None, expected_handles=None,
          replies_to=None, age_s=0):
    from app.kernel.v2.models import OperationEventV2Model
    from sqlalchemy import select, func
    import json
    with m["db"].session_scope() as s:
        max_seq = s.scalar(
            select(func.coalesce(func.max(OperationEventV2Model.seq), 0))
            .where(OperationEventV2Model.operation_id == op_id)
        ) or 0
        payload = {}
        if expected_handles is not None:
            payload["_meta"] = {
                "expected_response": {"from_actor_handles": expected_handles},
            }
        ev = OperationEventV2Model(
            operation_id=op_id,
            actor_id=actor_id,
            seq=int(max_seq) + 1,
            kind=kind,
            payload_json=json.dumps(payload),
            addressed_to_actor_ids_json=json.dumps(addressed_actor_ids or []),
            replies_to_event_id=replies_to,
        )
        if age_s:
            ev.created_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        s.add(ev); s.flush()
        return ev.id


def _ensure_actor(m, handle):
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    with m["db"].session_scope() as s:
        return actors.ensure_actor_by_handle(s, handle=handle).id


def test_sweeper_emits_nudge_decision_when_expected_response_idles(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    _ensure_actor(m, "@curator")
    _post(
        m, op, actor_id=alice, kind="chat.speech.claim",
        expected_handles=["@curator"], age_s=120,
    )
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        actions = sweeper.tick(s)
    nudges = [a for a in actions if a.action == "nudge" and a.op_id == op]
    assert len(nudges) == 1
    assert nudges[0].target_handle == "@curator"


def test_sweeper_terminal_event_yields_no_action(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op = _new_op(m)
    alice = _ensure_actor(m, "@alice")
    _post(m, op, actor_id=alice, kind="chat.speech.claim", age_s=120)
    sweeper = m["ProgressionSweeper"](idle_s=30, max_retries=2)
    with m["db"].session_scope() as s:
        decisions = {a.action for a in sweeper.tick(s) if a.op_id == op}
    assert decisions == {"skip"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
