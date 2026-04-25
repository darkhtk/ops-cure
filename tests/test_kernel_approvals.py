from __future__ import annotations

import time

import pytest


@pytest.fixture()
def approvals(app_env):
    from app.kernel.approvals import KernelApprovalService

    return KernelApprovalService()


def test_request_creates_pending_approval(app_env, approvals):
    from app.kernel.approvals import APPROVAL_STATUS_PENDING

    with app_env.db.session_scope() as db:
        record = approvals.request(
            db,
            space_id="thread-A",
            kind="remote_codex.exec_command",
            payload={"command": ["ls", "-la"], "cwd": "/tmp"},
            requested_by="codex",
        )

    assert record.status == APPROVAL_STATUS_PENDING
    assert record.space_id == "thread-A"
    assert record.kind == "remote_codex.exec_command"
    assert record.payload == {"command": ["ls", "-la"], "cwd": "/tmp"}
    assert record.requested_by == "codex"
    assert record.id


def test_request_rejects_missing_space_or_kind(app_env, approvals):
    with app_env.db.session_scope() as db:
        with pytest.raises(ValueError):
            approvals.request(db, space_id="", kind="x")
        with pytest.raises(ValueError):
            approvals.request(db, space_id="s", kind="")


def test_resolve_marks_approved_and_records_audit_fields(app_env, approvals):
    from app.kernel.approvals import APPROVAL_STATUS_APPROVED

    with app_env.db.session_scope() as db:
        record = approvals.request(db, space_id="s", kind="k", payload={"x": 1})

    with app_env.db.session_scope() as db:
        resolved = approvals.resolve(
            db,
            approval_id=record.id,
            resolution="approved",
            resolved_by="darkhtk",
            note="LGTM",
        )

    assert resolved is not None
    assert resolved.status == APPROVAL_STATUS_APPROVED
    assert resolved.resolution == "approved"
    assert resolved.resolved_by == "darkhtk"
    assert resolved.note == "LGTM"
    assert resolved.resolved_at is not None


def test_resolve_supports_codex_decision_superset(app_env, approvals):
    from app.kernel.approvals import APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED

    with app_env.db.session_scope() as db:
        a = approvals.request(db, space_id="s", kind="k")
        b = approvals.request(db, space_id="s", kind="k")
        c = approvals.request(db, space_id="s", kind="k")
        d = approvals.request(db, space_id="s", kind="k")

    cases = [
        (a.id, "approved", APPROVAL_STATUS_APPROVED),
        (b.id, "approved_for_session", APPROVAL_STATUS_APPROVED),
        (c.id, "rejected", APPROVAL_STATUS_REJECTED),
        (d.id, "abort", APPROVAL_STATUS_REJECTED),
    ]
    for approval_id, decision, expected_status in cases:
        with app_env.db.session_scope() as db:
            result = approvals.resolve(db, approval_id=approval_id, resolution=decision)
            assert result is not None
            assert result.resolution == decision
            assert result.status == expected_status


def test_resolve_is_idempotent_on_already_terminal_records(app_env, approvals):
    with app_env.db.session_scope() as db:
        record = approvals.request(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        first = approvals.resolve(db, approval_id=record.id, resolution="approved", resolved_by="A")
    with app_env.db.session_scope() as db:
        second = approvals.resolve(db, approval_id=record.id, resolution="rejected", resolved_by="B")
    assert first is not None and second is not None
    # The second resolve must NOT overwrite the first decision.
    assert second.resolution == first.resolution == "approved"
    assert second.resolved_by == "A"


def test_resolve_returns_none_for_unknown_id(app_env, approvals):
    with app_env.db.session_scope() as db:
        assert approvals.resolve(db, approval_id="does-not-exist", resolution="approved") is None


def test_list_pending_filters_by_space_and_kinds(app_env, approvals):
    with app_env.db.session_scope() as db:
        approvals.request(db, space_id="s1", kind="k1")
        approvals.request(db, space_id="s1", kind="k2")
        approvals.request(db, space_id="s2", kind="k1")

    with app_env.db.session_scope() as db:
        s1_all = approvals.list_pending(db, space_id="s1")
        s1_k1 = approvals.list_pending(db, space_id="s1", kinds=["k1"])
        s2_all = approvals.list_pending(db, space_id="s2")

    assert {(r.space_id, r.kind) for r in s1_all} == {("s1", "k1"), ("s1", "k2")}
    assert [(r.space_id, r.kind) for r in s1_k1] == [("s1", "k1")]
    assert [(r.space_id, r.kind) for r in s2_all] == [("s2", "k1")]


def test_list_pending_excludes_resolved_and_expired(app_env, approvals):
    with app_env.db.session_scope() as db:
        a = approvals.request(db, space_id="s", kind="k")
        b = approvals.request(db, space_id="s", kind="k")

    with app_env.db.session_scope() as db:
        approvals.resolve(db, approval_id=a.id, resolution="approved")

    with app_env.db.session_scope() as db:
        pending = approvals.list_pending(db, space_id="s")

    assert [r.id for r in pending] == [b.id]


def test_ttl_expires_pending_approvals_via_get(app_env, approvals):
    from app.kernel.approvals import APPROVAL_STATUS_EXPIRED

    with app_env.db.session_scope() as db:
        record = approvals.request(db, space_id="s", kind="k", ttl_seconds=1)

    time.sleep(1.2)

    with app_env.db.session_scope() as db:
        fetched = approvals.get(db, approval_id=record.id)
    assert fetched is not None
    assert fetched.status == APPROVAL_STATUS_EXPIRED


def test_resolve_after_expiry_short_circuits_to_expired(app_env, approvals):
    from app.kernel.approvals import APPROVAL_STATUS_EXPIRED

    with app_env.db.session_scope() as db:
        record = approvals.request(db, space_id="s", kind="k", ttl_seconds=1)

    time.sleep(1.2)

    with app_env.db.session_scope() as db:
        result = approvals.resolve(db, approval_id=record.id, resolution="approved")
    assert result is not None
    assert result.status == APPROVAL_STATUS_EXPIRED


def test_expire_due_marks_only_overdue_rows(app_env, approvals):
    with app_env.db.session_scope() as db:
        long = approvals.request(db, space_id="s", kind="k", ttl_seconds=120)
        short = approvals.request(db, space_id="s", kind="k", ttl_seconds=1)

    time.sleep(1.2)

    with app_env.db.session_scope() as db:
        expired_count = approvals.expire_due(db)
    assert expired_count == 1

    with app_env.db.session_scope() as db:
        long_record = approvals.get(db, approval_id=long.id)
        short_record = approvals.get(db, approval_id=short.id)
    assert long_record is not None and long_record.status == "pending"
    assert short_record is not None and short_record.status == "expired"


def test_freeform_resolution_string_routes_to_status(app_env, approvals):
    from app.kernel.approvals import APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED

    with app_env.db.session_scope() as db:
        a = approvals.request(db, space_id="s", kind="k")
        b = approvals.request(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        a_resolved = approvals.resolve(db, approval_id=a.id, resolution="deferred")
        b_resolved = approvals.resolve(db, approval_id=b.id, resolution="rejected-with-reason")
    assert a_resolved is not None and a_resolved.status == APPROVAL_STATUS_APPROVED  # default
    assert b_resolved is not None and b_resolved.status == APPROVAL_STATUS_REJECTED  # negative marker


def test_payload_round_trip_preserves_codex_apply_patch_shape(app_env, approvals):
    """The kernel approval store must be able to carry codex's typed
    approval params verbatim — that's the whole reason payload_json is
    a freeform JSON column instead of a fixed schema.
    """
    payload = {
        "callId": "call-123",
        "fileChanges": {
            "src/main.py": {"kind": "modify"},
            "src/util.py": {"kind": "add"},
        },
        "reason": "Refactor the entry point",
        "grantRoot": None,
    }
    with app_env.db.session_scope() as db:
        record = approvals.request(
            db,
            space_id="thread-A",
            kind="remote_codex.apply_patch",
            payload=payload,
        )
    with app_env.db.session_scope() as db:
        fetched = approvals.get(db, approval_id=record.id)
    assert fetched is not None
    assert fetched.payload == payload
