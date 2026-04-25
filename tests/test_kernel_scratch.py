from __future__ import annotations

import time

import pytest


@pytest.fixture()
def scratch(app_env):
    from app.kernel.scratch import KernelScratchService

    return KernelScratchService()


def test_scratch_set_and_get_round_trip(app_env, scratch):
    with app_env.db.session_scope() as db:
        scratch.set(db, key="dedup.command-1", actor_id="homedev", value={"seen": True})

    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="dedup.command-1", actor_id="homedev") == {"seen": True}


def test_scratch_get_returns_default_for_missing_keys(app_env, scratch):
    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="never-set") is None
        assert scratch.get(db, key="never-set", default="missing") == "missing"


def test_scratch_overwrites_existing_value_for_same_scope_triple(app_env, scratch):
    with app_env.db.session_scope() as db:
        scratch.set(db, key="counter", actor_id="a", value=1)
        scratch.set(db, key="counter", actor_id="a", value=2)

    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="counter", actor_id="a") == 2


def test_scratch_isolates_distinct_actor_space_scopes(app_env, scratch):
    with app_env.db.session_scope() as db:
        scratch.set(db, key="last_seen", actor_id="homedev", value="A")
        scratch.set(db, key="last_seen", actor_id="laptop", value="B")
        scratch.set(db, key="last_seen", space_id="space-1", value="C")
        scratch.set(db, key="last_seen", actor_id="homedev", space_id="space-1", value="D")

    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="last_seen", actor_id="homedev") == "A"
        assert scratch.get(db, key="last_seen", actor_id="laptop") == "B"
        assert scratch.get(db, key="last_seen", space_id="space-1") == "C"
        assert scratch.get(db, key="last_seen", actor_id="homedev", space_id="space-1") == "D"


def test_scratch_has_reflects_presence(app_env, scratch):
    with app_env.db.session_scope() as db:
        assert scratch.has(db, key="flag", actor_id="x") is False
        scratch.set(db, key="flag", actor_id="x", value=False)
        assert scratch.has(db, key="flag", actor_id="x") is True
        scratch.delete(db, key="flag", actor_id="x")

    with app_env.db.session_scope() as db:
        assert scratch.has(db, key="flag", actor_id="x") is False


def test_scratch_ttl_expires_entries(app_env, scratch):
    with app_env.db.session_scope() as db:
        scratch.set(db, key="ephemeral", actor_id="x", value="hold", ttl_seconds=1)

    # Within TTL: visible
    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="ephemeral", actor_id="x") == "hold"

    time.sleep(1.2)

    # After TTL: filtered out by `get` even before cleanup_expired runs
    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="ephemeral", actor_id="x") is None
        assert scratch.has(db, key="ephemeral", actor_id="x") is False


def test_scratch_cleanup_expired_removes_only_expired_rows(app_env, scratch):
    with app_env.db.session_scope() as db:
        scratch.set(db, key="alive", actor_id="x", value=1)
        scratch.set(db, key="dead", actor_id="x", value=2, ttl_seconds=1)

    time.sleep(1.2)

    with app_env.db.session_scope() as db:
        removed = scratch.cleanup_expired(db)

    assert removed == 1

    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="alive", actor_id="x") == 1
        assert scratch.has(db, key="dead", actor_id="x") is False


def test_scratch_non_positive_ttl_acts_as_delete(app_env, scratch):
    with app_env.db.session_scope() as db:
        scratch.set(db, key="x", actor_id="a", value="hello")
    with app_env.db.session_scope() as db:
        scratch.set(db, key="x", actor_id="a", value="world", ttl_seconds=0)
    with app_env.db.session_scope() as db:
        assert scratch.has(db, key="x", actor_id="a") is False


def test_scratch_value_serializes_complex_python_types(app_env, scratch):
    payload = {
        "list": [1, 2, 3],
        "nested": {"flag": True, "score": 0.5},
        "none": None,
    }
    with app_env.db.session_scope() as db:
        scratch.set(db, key="complex", actor_id="x", value=payload)

    with app_env.db.session_scope() as db:
        assert scratch.get(db, key="complex", actor_id="x") == payload


def test_scratch_delete_returns_false_for_missing_key(app_env, scratch):
    with app_env.db.session_scope() as db:
        assert scratch.delete(db, key="not-there", actor_id="x") is False
