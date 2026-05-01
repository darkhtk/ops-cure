"""F9: capability-based authorization replaces string-match callback."""
from __future__ import annotations

import sys

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
    from app.kernel.v2 import (
        CapabilityService, ActorService,
        CAP_CONVERSATION_OPEN, CAP_TASK_APPROVE_DESTRUCTIVE,
        CAP_CONVERSATION_CLOSE,
    )
    db.init_db()
    return locals() | {"db": db}


def test_human_default_capabilities_include_open_close_not_destructive(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    actors = m["ActorService"]()
    cap = m["CapabilityService"]()
    with db.session_scope() as session:
        actors.ensure_actor_by_handle(
            session, handle="@operator", display_name="op", kind="human",
        )
    with db.session_scope() as session:
        assert cap.actor_can(session, actor_handle="@operator", capability=m["CAP_CONVERSATION_OPEN"])
        assert cap.actor_can(session, actor_handle="@operator", capability=m["CAP_CONVERSATION_CLOSE"])
        assert not cap.actor_can(
            session, actor_handle="@operator", capability=m["CAP_TASK_APPROVE_DESTRUCTIVE"],
        )


def test_ai_default_capabilities_exclude_unrestricted_close(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    actors = m["ActorService"]()
    cap = m["CapabilityService"]()
    with db.session_scope() as session:
        actors.ensure_actor_by_handle(session, handle="@claude-pca", kind="ai")
    with db.session_scope() as session:
        assert cap.actor_can(session, actor_handle="@claude-pca", capability=m["CAP_CONVERSATION_OPEN"])
        # ai default cannot close arbitrary conversations -- only ones it
        # opened (CAP_CONVERSATION_CLOSE_OPENER)
        assert not cap.actor_can(
            session, actor_handle="@claude-pca", capability=m["CAP_CONVERSATION_CLOSE"],
        )


def test_grant_and_revoke_round_trip(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    with db.session_scope() as session:
        new = cap.grant(
            session,
            actor_handle="@operator",
            capabilities=[m["CAP_TASK_APPROVE_DESTRUCTIVE"]],
        )
        assert m["CAP_TASK_APPROVE_DESTRUCTIVE"] in new
    with db.session_scope() as session:
        assert cap.actor_can(
            session, actor_handle="@operator",
            capability=m["CAP_TASK_APPROVE_DESTRUCTIVE"],
        )
    with db.session_scope() as session:
        kept = cap.revoke(
            session,
            actor_handle="@operator",
            capabilities=[m["CAP_TASK_APPROVE_DESTRUCTIVE"]],
        )
        assert m["CAP_TASK_APPROVE_DESTRUCTIVE"] not in kept
    with db.session_scope() as session:
        assert not cap.actor_can(
            session, actor_handle="@operator",
            capability=m["CAP_TASK_APPROVE_DESTRUCTIVE"],
        )


def test_explicit_grant_disables_kind_defaults(tmp_path, monkeypatch):
    """Once explicit caps are set, kind defaults stop applying. An
    operator who explicitly granted only ``conversation.open`` cannot
    fall back to the human default set."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    with db.session_scope() as session:
        cap.grant(
            session, actor_handle="@scoped",
            capabilities=[m["CAP_CONVERSATION_OPEN"]],
        )
    with db.session_scope() as session:
        assert cap.actor_can(session, actor_handle="@scoped", capability=m["CAP_CONVERSATION_OPEN"])
        # human default would include CONVERSATION_CLOSE -- but the
        # actor was bootstrapped via grant() (kind=ai default) and now
        # has an explicit cap list, so close is denied.
        assert not cap.actor_can(
            session, actor_handle="@scoped", capability=m["CAP_CONVERSATION_CLOSE"],
        )


def test_unknown_actor_denied(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    cap = m["CapabilityService"]()
    with db.session_scope() as session:
        assert not cap.actor_can(session, actor_handle="@ghost", capability=m["CAP_CONVERSATION_OPEN"])
