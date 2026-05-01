"""F2: Actor 1st-class wiring -- caller -> Actor mapping."""
from __future__ import annotations

import sys

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.kernel.v2 import ActorService
    db.init_db()
    return {"db": db, "service": ActorService()}


def test_ensure_actor_by_handle_get_or_create(tmp_path, monkeypatch):
    """First call inserts; second call with same handle returns same row."""
    m = _bootstrap(tmp_path, monkeypatch)
    db, service = m["db"], m["service"]
    with db.session_scope() as session:
        a = service.ensure_actor_by_handle(session, handle="@claude-pca")
        a_id = a.id
    with db.session_scope() as session:
        b = service.ensure_actor_by_handle(session, handle="@claude-pca")
        assert b.id == a_id
        assert b.status == "online"  # presence updated


def test_actor_for_caller_with_client_id(tmp_path, monkeypatch):
    """Asserted client_id becomes a v2 actor with @-prefixed handle."""
    m = _bootstrap(tmp_path, monkeypatch)
    db, service = m["db"], m["service"]
    with db.session_scope() as session:
        a = service.actor_for_caller(session, asserted_client_id="claude-pca")
        assert a.handle == "@claude-pca"
        assert a.kind == "ai"


def test_actor_for_caller_without_client_id_is_operator(tmp_path, monkeypatch):
    """Bare shared-token call -> human operator actor."""
    m = _bootstrap(tmp_path, monkeypatch)
    db, service = m["db"], m["service"]
    with db.session_scope() as session:
        a = service.actor_for_caller(session, asserted_client_id=None)
        assert a.handle == "@operator"
        assert a.kind == "human"
    # idempotent across calls
    with db.session_scope() as session:
        b = service.actor_for_caller(session, asserted_client_id="")
        assert b.handle == "@operator"
