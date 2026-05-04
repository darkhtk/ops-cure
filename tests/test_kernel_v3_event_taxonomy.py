"""P15: event-kind taxonomy is transport-prefix-agnostic.

Pins:
  - is_speech_kind / speech_action / _category_of accept both
    chat.* (legacy), bare-category, and a hypothetical cli.* prefix.
  - repository.last_speech_event_for_op + count_speech_events match
    across prefixes.
  - back-compat: existing chat.* event flows still pass.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import NAS_BRIDGE_ROOT


# ---------------------------------------------------------------------------
# Pure helpers — no DB
# ---------------------------------------------------------------------------


def _import_contract():
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
    os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
    from app.kernel.v2 import contract
    return contract


def test_is_speech_kind_chat_prefix():
    c = _import_contract()
    assert c.is_speech_kind("chat.speech.claim")
    assert c.is_speech_kind("chat.speech.move_close")
    assert c.is_speech_kind("chat.speech.defer")


def test_is_speech_kind_bare_category():
    c = _import_contract()
    assert c.is_speech_kind("speech.claim")
    assert c.is_speech_kind("speech.ratify")


def test_is_speech_kind_alternate_transport():
    c = _import_contract()
    assert c.is_speech_kind("cli.speech.claim")
    assert c.is_speech_kind("webhook.speech.evidence")


def test_is_speech_kind_negative():
    c = _import_contract()
    assert not c.is_speech_kind("chat.system.nudge")
    assert not c.is_speech_kind("chat.task.claimed")
    assert not c.is_speech_kind("chat.conversation.opened")
    assert not c.is_speech_kind("speech")  # missing action token
    assert not c.is_speech_kind("")
    assert not c.is_speech_kind("garbage.foo.bar")


def test_speech_action_extracts_action_token():
    c = _import_contract()
    assert c.speech_action("chat.speech.claim") == "claim"
    assert c.speech_action("chat.speech.move_close") == "move_close"
    assert c.speech_action("speech.ratify") == "ratify"
    assert c.speech_action("cli.speech.evidence") == "evidence"


def test_speech_action_none_for_non_speech():
    c = _import_contract()
    assert c.speech_action("chat.system.nudge") is None
    assert c.speech_action("chat.task.claimed") is None
    assert c.speech_action("garbage") is None
    assert c.speech_action("") is None


def test_category_of_recognizes_known_categories():
    c = _import_contract()
    assert c._category_of("chat.speech.claim") == "speech"
    assert c._category_of("chat.system.nudge") == "system"
    assert c._category_of("chat.task.claimed") == "task"
    assert c._category_of("chat.conversation.opened") == "conversation"


# ---------------------------------------------------------------------------
# Repository helpers — DB-backed
# ---------------------------------------------------------------------------


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
    from app.kernel.v2.actor_service import ActorService
    db.init_db()
    return locals()


def _new_op(m):
    from app.kernel.v2.models import OperationV2Model
    with m["db"].session_scope() as s:
        op = OperationV2Model(
            id=str(uuid.uuid4()),
            space_id="t",
            kind="task",
            title="t",
            state="open",
        )
        s.add(op); s.flush()
        return op.id


def _ensure(m, handle):
    repo = m["V2Repository"]()
    actors = m["ActorService"](repo)
    with m["db"].session_scope() as s:
        return actors.ensure_actor_by_handle(s, handle=handle).id


def _post(m, op_id, *, kind, actor_id, age_s=0):
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
            payload_json=json.dumps({}),
            addressed_to_actor_ids_json=json.dumps([]),
        )
        if age_s:
            ev.created_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        s.add(ev); s.flush()


def test_last_speech_event_matches_chat_prefix(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op = _new_op(m)
    a = _ensure(m, "@alice")
    _post(m, op, kind="chat.speech.claim", actor_id=a, age_s=10)
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        last = repo.last_speech_event_for_op(s, operation_id=op)
        assert last is not None
        assert last.kind == "chat.speech.claim"


def test_last_speech_event_matches_bare_category(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op = _new_op(m)
    a = _ensure(m, "@alice")
    _post(m, op, kind="speech.evidence", actor_id=a, age_s=10)
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        last = repo.last_speech_event_for_op(s, operation_id=op)
        assert last is not None
        assert last.kind == "speech.evidence"


def test_last_speech_event_matches_alternate_transport(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op = _new_op(m)
    a = _ensure(m, "@alice")
    _post(m, op, kind="cli.speech.claim", actor_id=a, age_s=10)
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        last = repo.last_speech_event_for_op(s, operation_id=op)
        assert last is not None
        assert last.kind == "cli.speech.claim"


def test_last_speech_skips_system_and_lifecycle(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op = _new_op(m)
    a = _ensure(m, "@alice")
    sys_a = _ensure(m, "@system")
    _post(m, op, kind="chat.speech.claim", actor_id=a, age_s=20)
    _post(m, op, kind="chat.system.nudge", actor_id=sys_a, age_s=10)
    _post(m, op, kind="chat.conversation.over_speech", actor_id=sys_a, age_s=5)
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        last = repo.last_speech_event_for_op(s, operation_id=op)
        assert last is not None
        assert last.kind == "chat.speech.claim", \
            "system / lifecycle events must not shadow the speech trigger"


def test_count_speech_events_across_transports(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    op = _new_op(m)
    a = _ensure(m, "@alice")
    sys_a = _ensure(m, "@system")
    _post(m, op, kind="chat.speech.claim", actor_id=a)
    _post(m, op, kind="cli.speech.evidence", actor_id=a)
    _post(m, op, kind="speech.ratify", actor_id=a)
    # noise that must NOT be counted
    _post(m, op, kind="chat.system.nudge", actor_id=sys_a)
    _post(m, op, kind="chat.task.claimed", actor_id=a)
    repo = m["V2Repository"]()
    with m["db"].session_scope() as s:
        assert repo.count_speech_events(s, operation_id=op) == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
