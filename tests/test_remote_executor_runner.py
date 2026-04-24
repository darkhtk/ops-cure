from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pc_launcher.connectors.remote_executor.runner import RunnerConfig, parse_args, run_cycle


class FailingDeviceAgent:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def poll_once(self) -> bool:
        raise self.error


class RecordingBridge:
    def __init__(self) -> None:
        self.claim_calls: list[tuple[str, str, int]] = []

    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
    ):
        self.claim_calls.append((machine_id, actor_id, lease_seconds))
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


def test_run_cycle_survives_device_agent_connection_error() -> None:
    bridge = RecordingBridge()
    session = SimpleNamespace(
        bridge=bridge,
        runtime=None,
        device_agent=FailingDeviceAgent(ConnectionError("remote end closed connection")),
    )

    worked = run_cycle(session, _config())

    assert worked is False


class ClaimingBridge:
    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
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
