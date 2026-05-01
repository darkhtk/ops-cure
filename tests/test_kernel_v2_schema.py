"""F1: Protocol v2 schema + Repository round-trip.

These tests prove the v2 schema is sound -- tables create cleanly,
all CRUD paths through V2Repository work, and the cross-table FK +
cascade behavior matches expectation. v1 paths are not touched, so
the full repo regression should still be green after this PR.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

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
    from app.kernel.v2 import models as v2_models
    from app.kernel.v2.repository import V2Repository

    db.init_db()

    return {"db": db, "v2": v2_models, "repo": V2Repository()}


def test_create_actor_round_trip(tmp_path, monkeypatch):
    """Insert an actor, look up by handle, capabilities round-trip."""
    modules = _bootstrap(tmp_path, monkeypatch)
    db = modules["db"]
    repo: "V2Repository" = modules["repo"]

    with db.session_scope() as session:
        actor = repo.insert_actor(
            session,
            handle="@alice", display_name="Alice", kind="human",
            capabilities=["close_inquiry", "approve_destructive"],
            status="online",
        )
        actor_id = actor.id

    with db.session_scope() as session:
        looked_up = repo.get_actor_by_handle(session, "@alice")
        assert looked_up is not None
        assert looked_up.id == actor_id
        assert looked_up.kind == "human"
        assert repo.actor_capabilities(looked_up) == [
            "close_inquiry", "approve_destructive",
        ]


def test_handle_uniqueness_enforced(tmp_path, monkeypatch):
    """Two actors cannot share a handle."""
    modules = _bootstrap(tmp_path, monkeypatch)
    db = modules["db"]
    repo: "V2Repository" = modules["repo"]
    with db.session_scope() as session:
        repo.insert_actor(session, handle="@alice", display_name="Alice")
    with pytest.raises(Exception):
        with db.session_scope() as session:
            repo.insert_actor(session, handle="@alice", display_name="Alice 2")


def test_operation_with_participants_and_event_chain(tmp_path, monkeypatch):
    """Full round-trip: actors + operation + participants + events.
    Verify the seq counter is monotonic per operation."""
    modules = _bootstrap(tmp_path, monkeypatch)
    db = modules["db"]
    repo: "V2Repository" = modules["repo"]
    v2 = modules["v2"]

    with db.session_scope() as session:
        alice = repo.insert_actor(session, handle="@alice", display_name="Alice", kind="human")
        bob = repo.insert_actor(session, handle="@claude-pca", display_name="Claude PC-A", kind="ai")
        alice_id, bob_id = alice.id, bob.id

    with db.session_scope() as session:
        op = repo.insert_operation(
            session, space_id="chat:thread-uuid-1",
            kind="task", title="Refactor auth middleware",
            metadata={"objective": "replace legacy session token storage"},
        )
        op_id = op.id

        repo.add_participant(session, operation_id=op_id, actor_id=alice_id, role="opener")
        repo.add_participant(session, operation_id=op_id, actor_id=bob_id, role="owner")

        e1 = repo.insert_event(
            session, operation_id=op_id, actor_id=alice_id, kind="speech.claim",
            payload={"text": "ready when you are"},
        )
        e2 = repo.insert_event(
            session, operation_id=op_id, actor_id=bob_id, kind="claim",
            payload={"lease_token": "abc", "lease_seconds": 120},
        )
        e3 = repo.insert_event(
            session, operation_id=op_id, actor_id=bob_id, kind="evidence",
            payload={"evidence_kind": "file_write", "summary": "patched middleware.py"},
            replies_to_event_id=e2.id,
        )
        assert e1.seq == 1
        assert e2.seq == 2
        assert e3.seq == 3

    with db.session_scope() as session:
        events = repo.list_events(session, operation_id=op_id)
        kinds = [e.kind for e in events]
        seqs = [e.seq for e in events]
        assert kinds == ["speech.claim", "claim", "evidence"]
        assert seqs == [1, 2, 3]
        # reply chain preserved
        assert events[2].replies_to_event_id == events[1].id


def test_addressed_and_private_event_round_trip(tmp_path, monkeypatch):
    """addressed_to_actor_ids and private_to_actor_ids round-trip
    through JSON. Empty private_to stores as NULL (semantic 'public');
    set means redacted-to."""
    modules = _bootstrap(tmp_path, monkeypatch)
    db = modules["db"]
    repo: "V2Repository" = modules["repo"]
    with db.session_scope() as session:
        alice = repo.insert_actor(session, handle="@alice", display_name="Alice")
        bob = repo.insert_actor(session, handle="@bob", display_name="Bob")
        carol = repo.insert_actor(session, handle="@carol", display_name="Carol")
        op = repo.insert_operation(session, space_id="chat:t1", kind="inquiry", title="?")
        # public address to multiple actors
        e_pub = repo.insert_event(
            session, operation_id=op.id, actor_id=alice.id, kind="speech.question",
            payload={"text": "anyone has the doc?"},
            addressed_to_actor_ids=[bob.id, carol.id],
        )
        # whisper to bob only
        e_whisper = repo.insert_event(
            session, operation_id=op.id, actor_id=alice.id, kind="speech.claim",
            payload={"text": "between us, this is risky"},
            private_to_actor_ids=[bob.id],
        )
        op_id, e_pub_id, e_whisper_id = op.id, e_pub.id, e_whisper.id
        bob_id, carol_id = bob.id, carol.id

    with db.session_scope() as session:
        events = repo.list_events(session, operation_id=op_id)
        by_id = {e.id: e for e in events}
        assert repo.event_addressed_to(by_id[e_pub_id]) == [bob_id, carol_id]
        assert repo.event_private_to(by_id[e_pub_id]) is None  # public
        assert repo.event_private_to(by_id[e_whisper_id]) == [bob_id]


def test_artifact_attached_to_event_and_cascades_with_operation(tmp_path, monkeypatch):
    """Insert artifacts under an event; deleting the operation
    cascades and removes events + artifacts together."""
    modules = _bootstrap(tmp_path, monkeypatch)
    db = modules["db"]
    repo: "V2Repository" = modules["repo"]
    v2 = modules["v2"]

    with db.session_scope() as session:
        alice = repo.insert_actor(session, handle="@alice", display_name="Alice")
        op = repo.insert_operation(session, space_id="chat:t1", kind="task", title="t")
        ev = repo.insert_event(
            session, operation_id=op.id, actor_id=alice.id, kind="evidence",
            payload={"evidence_kind": "screenshot"},
        )
        a1 = repo.insert_artifact(
            session, operation_id=op.id, event_id=ev.id,
            kind="screenshot", uri="nas://volume1/artifacts/abc.png",
            sha256="deadbeef" * 8, mime="image/png", size_bytes=4096,
            label="prod console showing the error",
        )
        a2 = repo.insert_artifact(
            session, operation_id=op.id, event_id=ev.id,
            kind="log", uri="nas://volume1/artifacts/log-1.txt",
            sha256="cafebabe" * 8, mime="text/plain", size_bytes=12345,
        )
        op_id = op.id
        ev_id = ev.id

    with db.session_scope() as session:
        for_event = repo.list_artifacts_for_event(session, event_id=ev_id)
        assert {a.kind for a in for_event} == {"screenshot", "log"}

    # Delete the operation; events and artifacts must vanish (cascade)
    with db.session_scope() as session:
        op = session.get(v2.OperationV2Model, op_id)
        session.delete(op)

    with db.session_scope() as session:
        artifacts = session.execute(
            select(v2.OperationArtifactV2Model).where(
                v2.OperationArtifactV2Model.operation_id == op_id
            )
        ).all()
        events = session.execute(
            select(v2.OperationEventV2Model).where(
                v2.OperationEventV2Model.operation_id == op_id
            )
        ).all()
        assert artifacts == []
        assert events == []


def test_operations_for_actor_inbox_query(tmp_path, monkeypatch):
    """The repository powers the future Inbox API: 'show all
    operations actor X is involved in, by role'."""
    modules = _bootstrap(tmp_path, monkeypatch)
    db = modules["db"]
    repo: "V2Repository" = modules["repo"]

    with db.session_scope() as session:
        alice = repo.insert_actor(session, handle="@alice", display_name="A")
        bob = repo.insert_actor(session, handle="@bob", display_name="B")

        op_alice_owns = repo.insert_operation(
            session, space_id="chat:t1", kind="task", title="task A",
        )
        op_alice_addressed = repo.insert_operation(
            session, space_id="chat:t1", kind="inquiry", title="q for alice",
        )
        op_bob_only = repo.insert_operation(
            session, space_id="chat:t1", kind="task", title="task B",
        )

        repo.add_participant(session, operation_id=op_alice_owns.id, actor_id=alice.id, role="owner")
        repo.add_participant(session, operation_id=op_alice_addressed.id, actor_id=alice.id, role="addressed")
        repo.add_participant(session, operation_id=op_bob_only.id, actor_id=bob.id, role="owner")
        alice_id = alice.id

    with db.session_scope() as session:
        # alice's full inbox
        all_alice = repo.operations_for_actor(session, actor_id=alice_id)
        kinds = sorted(role for _, role in all_alice)
        assert kinds == ["addressed", "owner"]

        # filter by role
        owned = repo.operations_for_actor(session, actor_id=alice_id, roles=["owner"])
        assert len(owned) == 1
        assert owned[0][1] == "owner"


def test_seq_uniqueness_constraint(tmp_path, monkeypatch):
    """The (operation_id, seq) UNIQUE makes sure the seq monotonicity
    invariant survives even pathological writes."""
    modules = _bootstrap(tmp_path, monkeypatch)
    db = modules["db"]
    repo: "V2Repository" = modules["repo"]
    v2 = modules["v2"]

    with db.session_scope() as session:
        alice = repo.insert_actor(session, handle="@alice", display_name="A")
        op = repo.insert_operation(session, space_id="chat:t1", kind="general", title="g")
        repo.insert_event(
            session, operation_id=op.id, actor_id=alice.id, kind="speech.claim",
            payload={},
        )
        op_id, alice_id = op.id, alice.id

    # Manually insert a duplicate seq=1 in a fresh session -- must fail on
    # constraint. Use SessionLocal directly (not session_scope) so the
    # post-IntegrityError rolled-back transaction does not also fail the
    # surrounding commit.
    bad_session = db.SessionLocal()
    try:
        bad = v2.OperationEventV2Model(
            operation_id=op_id, actor_id=alice_id, seq=1,
            kind="speech.claim", payload_json="{}",
            addressed_to_actor_ids_json="[]",
        )
        bad_session.add(bad)
        with pytest.raises(Exception):
            bad_session.flush()
    finally:
        bad_session.rollback()
        bad_session.close()
