from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pc_launcher.connectors.remote_executor.runner import (
    RunnerConfig,
    _should_wake_for_kernel_frame,
    parse_args,
    run_cycle,
)


class FailingDeviceAgent:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def poll_once(self) -> bool:
        raise self.error


class RecordingBridge:
    def __init__(self) -> None:
        self.claim_calls: list[tuple[str, str, int, tuple[str, ...]]] = []

    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
        exclude_origin_surfaces: list[str] | None = None,
    ):
        self.claim_calls.append(
            (
                machine_id,
                actor_id,
                lease_seconds,
                tuple(exclude_origin_surfaces or []),
            )
        )
        return None


class IdleDeviceAgent:
    def poll_once(self) -> bool:
        return False


class QuietDeviceAgent:
    def poll_once(self) -> bool:
        return False

    def mark_thread_dirty(self, thread_id: str) -> None:
        self.thread_id = thread_id

    def perform_sync(self, *, force: bool = False) -> None:
        self.forced = force


class FailingBridge:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
        exclude_origin_surfaces: list[str] | None = None,
    ):
        raise self.error


def _config() -> RunnerConfig:
    return RunnerConfig(
        project_file=Path(__file__),
        machine_id="homedev",
        actor_id="codex-executor",
        workdir=None,
        runtime_mode="current-thread",
        codex_thread_id="thread-1",
        poll_interval_seconds=5.0,
        lease_seconds=90,
        run_once=False,
    )


def test_parse_args_uses_tighter_default_poll_interval() -> None:
    config = parse_args([])

    assert config.poll_interval_seconds == 1.0


def test_should_wake_for_kernel_frame_on_remote_codex_command_envelope() -> None:
    """A generic kernel SSE frame carrying a ``remote_codex.command.*``
    envelope is the only actionable signal for the remote executor — the
    legacy ``ready`` / ``snapshot`` / ``machine`` channels are mapped to
    keepalive sync, not full run cycles.
    """
    queued_frame = {
        "event": "event",
        "data": {
            "cursor": "cur-1",
            "space_id": "remote_codex.machine:homedev",
            "event": {"id": "cmd-1", "kind": "remote_codex.command.queued"},
        },
    }
    assert _should_wake_for_kernel_frame(queued_frame)

    completed_frame = {
        "event": "event",
        "data": {
            "cursor": "cur-2",
            "space_id": "remote_codex.machine:homedev",
            "event": {"id": "cmd-1", "kind": "remote_codex.command.completed"},
        },
    }
    assert _should_wake_for_kernel_frame(completed_frame)


def test_should_wake_for_kernel_frame_ignores_open_and_heartbeats() -> None:
    """Connection markers (open / heartbeat / reset) and unrelated kinds
    must not fire ``run_cycle`` — keepalive maintenance is handled by the
    consume-loop's separate cadence.
    """
    open_frame = {"event": "open", "data": {"space_id": "remote_codex.machine:homedev"}}
    heartbeat_frame = {"event": "heartbeat", "data": {"space_id": "remote_codex.machine:homedev"}}
    reset_frame = {"event": "reset", "data": {"space_id": "remote_codex.machine:homedev"}}
    foreign_kind_frame = {
        "event": "event",
        "data": {"event": {"kind": "chat.message.created"}},
    }

    assert not _should_wake_for_kernel_frame(open_frame)
    assert not _should_wake_for_kernel_frame(heartbeat_frame)
    assert not _should_wake_for_kernel_frame(reset_frame)
    assert not _should_wake_for_kernel_frame(foreign_kind_frame)
    assert not _should_wake_for_kernel_frame({"event": "event", "data": None})


def test_run_cycle_survives_device_agent_connection_error() -> None:
    bridge = RecordingBridge()
    session = SimpleNamespace(
        bridge=bridge,
        runtime=None,
        device_agent=FailingDeviceAgent(ConnectionError("remote end closed connection")),
    )

    worked = run_cycle(session, _config())

    assert worked is False
    assert bridge.claim_calls == []


class ClaimingBridge:
    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
        exclude_origin_surfaces: list[str] | None = None,
    ):
        return {
            "id": "task-1",
            "machine_id": machine_id,
            "thread_id": "thread-1",
            "objective": "Do something",
            "success_criteria": {},
            "priority": "normal",
            "origin_surface": "browser",
            "owner_actor_id": actor_id,
            "current_assignment": {"lease_token": "lease-1"},
        }


class ExplodingRuntime:
    def execute_task(self, context):
        raise RuntimeError("runtime exploded")


def test_run_cycle_survives_task_execution_error() -> None:
    session = SimpleNamespace(
        bridge=ClaimingBridge(),
        runtime=ExplodingRuntime(),
        device_agent=QuietDeviceAgent(),
    )

    worked = run_cycle(session, _config())

    assert worked is False


def test_run_cycle_survives_remote_task_claim_error() -> None:
    session = SimpleNamespace(
        bridge=FailingBridge(ConnectionError("temporary bridge outage")),
        runtime=None,
        device_agent=IdleDeviceAgent(),
    )

    worked = run_cycle(session, _config())

    assert worked is False


def test_run_cycle_excludes_browser_origin_from_generic_task_claim() -> None:
    bridge = RecordingBridge()
    session = SimpleNamespace(
        bridge=bridge,
        runtime=None,
        device_agent=IdleDeviceAgent(),
    )

    worked = run_cycle(session, _config())

    assert worked is False
    assert bridge.claim_calls == [
        ("homedev", "codex-executor", 90, ("browser",)),
    ]
