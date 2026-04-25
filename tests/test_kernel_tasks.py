from __future__ import annotations

import time

import pytest


@pytest.fixture()
def tasks(app_env):
    from app.kernel.tasks import KernelTaskService

    return KernelTaskService()


def test_enqueue_creates_queued_record(app_env, tasks):
    from app.kernel.tasks import TASK_STATUS_QUEUED

    with app_env.db.session_scope() as db:
        record = tasks.enqueue(
            db,
            space_id="thread-A",
            kind="orchestration.session_launch",
            payload={"profile": "demo"},
            requested_by="darkhtk",
        )

    assert record.status == TASK_STATUS_QUEUED
    assert record.space_id == "thread-A"
    assert record.kind == "orchestration.session_launch"
    assert record.payload == {"profile": "demo"}
    assert record.owner_actor_id is None
    assert record.lease_token is None


def test_enqueue_rejects_missing_space_or_kind(app_env, tasks):
    with app_env.db.session_scope() as db:
        with pytest.raises(ValueError):
            tasks.enqueue(db, space_id="", kind="x")
        with pytest.raises(ValueError):
            tasks.enqueue(db, space_id="s", kind="")


def test_claim_next_returns_oldest_queued_task(app_env, tasks):
    with app_env.db.session_scope() as db:
        first = tasks.enqueue(db, space_id="s", kind="k")
        time.sleep(0.01)
        tasks.enqueue(db, space_id="s", kind="k")

    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)

    assert claim is not None
    assert claim.task.id == first.id
    assert claim.task.status == "claimed"
    assert claim.task.owner_actor_id == "worker-A"
    assert claim.lease_token
    assert claim.task.claim_count == 1


def test_claim_next_honors_priority_order(app_env, tasks):
    with app_env.db.session_scope() as db:
        low = tasks.enqueue(db, space_id="s", kind="k", priority=0)
        time.sleep(0.01)
        high = tasks.enqueue(db, space_id="s", kind="k", priority=10)

    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)

    assert claim is not None
    assert claim.task.id == high.id
    assert claim.task.id != low.id


def test_claim_next_returns_none_when_queue_empty(app_env, tasks):
    with app_env.db.session_scope() as db:
        assert tasks.claim_next(db, actor_id="worker-A", lease_seconds=60) is None


def test_claim_next_filters_by_space_and_kinds(app_env, tasks):
    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s1", kind="k1")
        tasks.enqueue(db, space_id="s1", kind="k2")
        tasks.enqueue(db, space_id="s2", kind="k1")

    with app_env.db.session_scope() as db:
        s2_claim = tasks.claim_next(
            db,
            space_id="s2",
            actor_id="worker-A",
            lease_seconds=60,
        )

    assert s2_claim is not None
    assert s2_claim.task.space_id == "s2"
    assert s2_claim.task.kind == "k1"

    with app_env.db.session_scope() as db:
        k2_claim = tasks.claim_next(
            db,
            kinds=["k2"],
            actor_id="worker-A",
            lease_seconds=60,
        )

    assert k2_claim is not None
    assert k2_claim.task.kind == "k2"


def test_two_claims_in_a_row_pick_distinct_tasks(app_env, tasks):
    """Sanity check that the claim transition is atomic enough that a
    second claim against the same queue gets a different task — not a
    full concurrency test, but enough to lock down the basic isolation
    that the SQLite-backed test suite can verify deterministically.
    """
    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s", kind="k")
        tasks.enqueue(db, space_id="s", kind="k")

    with app_env.db.session_scope() as db:
        a = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)
    with app_env.db.session_scope() as db:
        b = tasks.claim_next(db, actor_id="worker-B", lease_seconds=60)

    assert a is not None and b is not None
    assert a.task.id != b.task.id
    assert a.task.owner_actor_id == "worker-A"
    assert b.task.owner_actor_id == "worker-B"


def test_heartbeat_extends_lease_and_can_advance_to_executing(app_env, tasks):
    from app.kernel.tasks import TASK_STATUS_EXECUTING

    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)
    assert claim is not None
    original_expiry = claim.task.lease_expires_at

    time.sleep(0.05)

    with app_env.db.session_scope() as db:
        beat = tasks.heartbeat(
            db,
            task_id=claim.task.id,
            lease_token=claim.lease_token,
            lease_seconds=120,
            status=TASK_STATUS_EXECUTING,
        )

    assert beat.status == TASK_STATUS_EXECUTING
    assert beat.lease_expires_at is not None
    assert beat.lease_expires_at > original_expiry


def test_heartbeat_rejects_stale_lease_token(app_env, tasks):
    from app.kernel.tasks import TaskLeaseError

    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)

    with app_env.db.session_scope() as db:
        with pytest.raises(TaskLeaseError):
            tasks.heartbeat(
                db,
                task_id=claim.task.id,
                lease_token="not-the-real-token",
                lease_seconds=60,
            )


def test_complete_marks_terminal_and_writes_result(app_env, tasks):
    from app.kernel.tasks import TASK_STATUS_COMPLETED

    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)

    with app_env.db.session_scope() as db:
        completed = tasks.complete(
            db,
            task_id=claim.task.id,
            lease_token=claim.lease_token,
            result={"ok": True, "output": "hello"},
        )

    assert completed.status == TASK_STATUS_COMPLETED
    assert completed.result == {"ok": True, "output": "hello"}
    assert completed.lease_expires_at is None
    assert completed.completed_at is not None


def test_fail_marks_terminal_and_writes_error(app_env, tasks):
    from app.kernel.tasks import TASK_STATUS_FAILED

    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)

    with app_env.db.session_scope() as db:
        failed = tasks.fail(
            db,
            task_id=claim.task.id,
            lease_token=claim.lease_token,
            error={"kind": "timeout", "message": "no response"},
        )

    assert failed.status == TASK_STATUS_FAILED
    assert failed.error == {"kind": "timeout", "message": "no response"}


def test_cancel_unclaimed_task_marks_cancelled(app_env, tasks):
    from app.kernel.tasks import TASK_STATUS_CANCELLED

    with app_env.db.session_scope() as db:
        record = tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        cancelled = tasks.cancel(db, task_id=record.id, reason="user-cancelled")
    assert cancelled is not None
    assert cancelled.status == TASK_STATUS_CANCELLED
    assert cancelled.error is not None
    assert cancelled.error["reason"] == "user-cancelled"


def test_cancel_is_idempotent_on_terminal_records(app_env, tasks):
    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=60)
    with app_env.db.session_scope() as db:
        completed = tasks.complete(
            db,
            task_id=claim.task.id,
            lease_token=claim.lease_token,
        )
    with app_env.db.session_scope() as db:
        again = tasks.cancel(db, task_id=claim.task.id, reason="late")
    assert again is not None
    # Already-terminal record stays whatever it was.
    assert again.status == completed.status


def test_release_expired_leases_returns_lapsed_tasks_to_queue(app_env, tasks):
    from app.kernel.tasks import TASK_STATUS_QUEUED

    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        claim = tasks.claim_next(db, actor_id="worker-A", lease_seconds=1)
    assert claim is not None

    time.sleep(1.2)

    with app_env.db.session_scope() as db:
        released = tasks.release_expired_leases(db)
    assert released == 1

    with app_env.db.session_scope() as db:
        record = tasks.get(db, task_id=claim.task.id)
    assert record is not None
    assert record.status == TASK_STATUS_QUEUED
    assert record.owner_actor_id is None
    assert record.lease_token is None


def test_claim_next_implicitly_sweeps_expired_leases(app_env, tasks):
    """A second worker claiming after a lease lapse should pick up the
    abandoned task without an external sweeper having to run.
    """
    with app_env.db.session_scope() as db:
        original = tasks.enqueue(db, space_id="s", kind="k")
    with app_env.db.session_scope() as db:
        first = tasks.claim_next(db, actor_id="worker-A", lease_seconds=1)
    assert first is not None and first.task.id == original.id

    time.sleep(1.2)

    with app_env.db.session_scope() as db:
        second = tasks.claim_next(db, actor_id="worker-B", lease_seconds=60)
    assert second is not None
    assert second.task.id == original.id
    assert second.task.owner_actor_id == "worker-B"
    assert second.task.claim_count == 2  # we re-claimed the same task


def test_list_filters_compose(app_env, tasks):
    from app.kernel.tasks import TASK_STATUS_QUEUED

    with app_env.db.session_scope() as db:
        tasks.enqueue(db, space_id="s1", kind="k1")
        tasks.enqueue(db, space_id="s1", kind="k2")
        tasks.enqueue(db, space_id="s2", kind="k1")
    with app_env.db.session_scope() as db:
        s1 = tasks.list(db, space_id="s1")
        s1_k1 = tasks.list(db, space_id="s1", kinds=["k1"])
        all_queued = tasks.list(db, statuses=[TASK_STATUS_QUEUED])

    assert {t.kind for t in s1} == {"k1", "k2"}
    assert [t.kind for t in s1_k1] == ["k1"]
    assert len(all_queued) == 3
