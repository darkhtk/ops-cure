from __future__ import annotations

from datetime import datetime, timezone


def _machine_payload(machine_id: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "machineId": machine_id,
        "displayName": machine_id,
        "source": "agent",
        "activeTransport": "standalone-app-server",
        "runtimeMode": "standalone-app-server",
        "runtimeAvailable": True,
        "capabilities": {"liveControl": True},
        "lastSeenAt": now,
        "lastSyncAt": now,
    }


def _thread_payload(thread_id: str) -> dict:
    return {
        "id": thread_id,
        "title": "Mirror test thread",
        "cwd": "C:/repo",
        "rolloutPath": "C:/repo/.codex/rollout.jsonl",
        "updatedAtMs": 1700000000000,
        "createdAtMs": 1699999999000,
        "source": "app-server",
        "modelProvider": "openai",
        "model": "gpt-5.4",
        "reasoningEffort": "medium",
        "cliVersion": "1.0.0",
        "firstUserMessage": "first",
        "status": {"type": "active"},
    }


def test_enqueue_command_mirrors_into_kernel_tasks(app_env):
    """A remote_codex command enqueue must also create a generic
    ``KernelTaskModel`` row keyed off the same primary key, so the
    /api/kernel/tasks surface can serve the same record without an
    extra translation layer.
    """
    from app.behaviors.remote_codex.state_service import (
        REMOTE_CODEX_COMMAND_KIND,
        RemoteCodexStateService,
        remote_codex_machine_space_id,
    )
    from app.kernel.tasks import KernelTaskService, TASK_STATUS_QUEUED

    state_service = RemoteCodexStateService()
    state_service.apply_agent_sync(
        machine=_machine_payload("machine-mirror"),
        threads=[_thread_payload("thread-mirror")],
        snapshots=[],
    )

    enqueued = state_service.enqueue_command(
        command_type="turn.start",
        machine_id="machine-mirror",
        thread_id="thread-mirror",
        requested_by={"actorId": "browser", "authMethod": "token"},
        prompt="hello",
    )

    kernel_service = KernelTaskService()
    with app_env.db.session_scope() as db:
        record = kernel_service.get(db, task_id=enqueued["commandId"])

    assert record is not None
    assert record.id == enqueued["commandId"]
    assert record.space_id == remote_codex_machine_space_id("machine-mirror")
    assert record.kind == REMOTE_CODEX_COMMAND_KIND
    assert record.status == TASK_STATUS_QUEUED
    assert record.payload.get("machine_id") == "machine-mirror"
    assert record.payload.get("thread_id") == "thread-mirror"
    assert record.payload.get("command_type") == "turn.start"


def test_claim_next_command_mirrors_status_into_kernel_tasks(app_env):
    """Claiming a command on the legacy table must drive the kernel
    task row to ``claimed`` with the same worker as owner.
    """
    from app.behaviors.remote_codex.state_service import RemoteCodexStateService
    from app.kernel.tasks import KernelTaskService, TASK_STATUS_CLAIMED

    state_service = RemoteCodexStateService()
    state_service.apply_agent_sync(
        machine=_machine_payload("machine-claim"),
        threads=[_thread_payload("thread-claim")],
        snapshots=[],
    )
    enqueued = state_service.enqueue_command(
        command_type="turn.start",
        machine_id="machine-claim",
        thread_id="thread-claim",
        requested_by={"actorId": "browser"},
        prompt="claim me",
    )

    state_service.claim_next_command(machine_id="machine-claim", worker_id="worker-A")

    kernel_service = KernelTaskService()
    with app_env.db.session_scope() as db:
        record = kernel_service.get(db, task_id=enqueued["commandId"])

    assert record is not None
    assert record.status == TASK_STATUS_CLAIMED
    assert record.owner_actor_id == "worker-A"
    assert record.claim_count >= 1


def test_complete_command_mirrors_terminal_status_and_result(app_env):
    """Completing the legacy command must drive the kernel task row to
    ``completed`` with the same result blob.
    """
    from app.behaviors.remote_codex.state_service import RemoteCodexStateService
    from app.kernel.tasks import KernelTaskService, TASK_STATUS_COMPLETED

    state_service = RemoteCodexStateService()
    state_service.apply_agent_sync(
        machine=_machine_payload("machine-complete"),
        threads=[_thread_payload("thread-complete")],
        snapshots=[],
    )
    enqueued = state_service.enqueue_command(
        command_type="turn.start",
        machine_id="machine-complete",
        thread_id="thread-complete",
        requested_by={"actorId": "browser"},
        prompt="complete me",
    )
    state_service.claim_next_command(machine_id="machine-complete", worker_id="worker-A")
    state_service.complete_command(
        enqueued["commandId"],
        worker_id="worker-A",
        result={"ok": True, "turnId": "turn-1"},
    )

    kernel_service = KernelTaskService()
    with app_env.db.session_scope() as db:
        record = kernel_service.get(db, task_id=enqueued["commandId"])

    assert record is not None
    assert record.status == TASK_STATUS_COMPLETED
    assert record.result == {"ok": True, "turnId": "turn-1"}


def test_fail_command_mirrors_terminal_status_and_error(app_env):
    """Failing the legacy command must drive the kernel task row to
    ``failed`` with the same error blob.
    """
    from app.behaviors.remote_codex.state_service import RemoteCodexStateService
    from app.kernel.tasks import KernelTaskService, TASK_STATUS_FAILED

    state_service = RemoteCodexStateService()
    state_service.apply_agent_sync(
        machine=_machine_payload("machine-fail"),
        threads=[_thread_payload("thread-fail")],
        snapshots=[],
    )
    enqueued = state_service.enqueue_command(
        command_type="turn.start",
        machine_id="machine-fail",
        thread_id="thread-fail",
        requested_by={"actorId": "browser"},
        prompt="fail me",
    )
    state_service.claim_next_command(machine_id="machine-fail", worker_id="worker-A")
    state_service.fail_command(
        enqueued["commandId"],
        worker_id="worker-A",
        error={"kind": "boom", "message": "something exploded"},
    )

    kernel_service = KernelTaskService()
    with app_env.db.session_scope() as db:
        record = kernel_service.get(db, task_id=enqueued["commandId"])

    assert record is not None
    assert record.status == TASK_STATUS_FAILED
    assert record.error is not None
    assert record.error.get("kind") == "boom"


def test_mirror_failure_does_not_break_legacy_command_write(app_env):
    """If the kernel mirror raises, the legacy command write must still
    succeed. The kernel side is best-effort during the migration window.
    """
    from app.behaviors.remote_codex.state_service import RemoteCodexStateService

    class ExplodingTaskService:
        def enqueue(self, *args, **kwargs):
            raise RuntimeError("simulated kernel task outage")

    state_service = RemoteCodexStateService(
        kernel_task_service=ExplodingTaskService(),
    )
    state_service.apply_agent_sync(
        machine=_machine_payload("machine-isolated"),
        threads=[_thread_payload("thread-isolated")],
        snapshots=[],
    )

    enqueued = state_service.enqueue_command(
        command_type="turn.start",
        machine_id="machine-isolated",
        thread_id="thread-isolated",
        requested_by={"actorId": "browser"},
        prompt="mirror is broken but legacy must queue",
    )

    assert enqueued["status"] == "queued"
    assert enqueued["machineId"] == "machine-isolated"
