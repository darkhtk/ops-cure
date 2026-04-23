from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from ...bridge_client import BridgeClient
    from ...config_loader import load_project
    from ...connectors.chat_participant.runtime import (
        CodexAppServerProcessClient,
        CodexCurrentThreadRuntimeConfig,
    )
    from ...process_io import configure_utf8_stdio
    from .bridge import BridgeRemoteExecutorClient
    from .device_agent import LocalCodexBackend, RemoteCodexDeviceAgent, compact_text
    from .runtime import (
        CodexCliRemoteExecutorRuntime,
        CodexCurrentThreadRemoteExecutorRuntime,
        ExecutionTaskContext,
        RemoteExecutorRuntime,
        _resolve_executable,
        _runtime_env_var,
    )
except ImportError:  # pragma: no cover - script mode support
    from pc_launcher.bridge_client import BridgeClient
    from pc_launcher.config_loader import load_project
    from pc_launcher.connectors.chat_participant.runtime import (
        CodexAppServerProcessClient,
        CodexCurrentThreadRuntimeConfig,
    )
    from pc_launcher.process_io import configure_utf8_stdio
    from pc_launcher.connectors.remote_executor.bridge import BridgeRemoteExecutorClient
    from pc_launcher.connectors.remote_executor.device_agent import LocalCodexBackend, RemoteCodexDeviceAgent, compact_text
    from pc_launcher.connectors.remote_executor.runtime import (
        CodexCliRemoteExecutorRuntime,
        CodexCurrentThreadRemoteExecutorRuntime,
        ExecutionTaskContext,
        RemoteExecutorRuntime,
        _resolve_executable,
        _runtime_env_var,
    )


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RunnerConfig:
    project_file: Path
    machine_id: str
    actor_id: str
    workdir: str | None
    runtime_mode: str
    codex_thread_id: str | None
    poll_interval_seconds: float
    lease_seconds: int
    run_once: bool


@dataclass(slots=True)
class RunnerSession:
    bridge: BridgeRemoteExecutorClient
    runtime: RemoteExecutorRuntime
    device_agent: RemoteCodexDeviceAgent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local Codex remote executor against Opscure remote tasks.",
    )
    parser.add_argument(
        "--project-file",
        default=str(Path(__file__).resolve().parents[2] / "projects" / "remote_executor" / "project.yaml"),
        help="Path to the pc_launcher project.yaml file that defines the bridge connection.",
    )
    parser.add_argument(
        "--machine-id",
        default=os.getenv("REMOTE_EXECUTOR_MACHINE_ID") or socket.gethostname().lower(),
        help="Machine id used to claim remote tasks.",
    )
    parser.add_argument(
        "--actor-id",
        default=os.getenv("REMOTE_EXECUTOR_ACTOR_ID") or "codex-executor",
        help="Executor actor id recorded on remote tasks.",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Optional working directory for the local Codex runtime. Defaults to the project workdir.",
    )
    parser.add_argument(
        "--runtime-mode",
        choices=["auto", "cli", "current-thread"],
        default=os.getenv("REMOTE_EXECUTOR_RUNTIME_MODE", "auto"),
        help="Runtime backend for executing remote tasks.",
    )
    parser.add_argument(
        "--codex-thread-id",
        default=os.getenv("REMOTE_EXECUTOR_CODEX_THREAD_ID") or os.getenv("CODEX_THREAD_ID"),
        help="Existing Codex thread id to use when runtime-mode is current-thread.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Idle poll interval when no remote task is available.",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=90,
        help="Lease duration for claimed remote tasks.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Inspect and execute at most one queued remote task before exiting.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> RunnerConfig:
    args = build_parser().parse_args(argv)
    return RunnerConfig(
        project_file=Path(args.project_file).resolve(),
        machine_id=str(args.machine_id).strip(),
        actor_id=str(args.actor_id).strip(),
        workdir=args.workdir,
        runtime_mode=str(args.runtime_mode),
        codex_thread_id=args.codex_thread_id or None,
        poll_interval_seconds=max(1.0, float(args.poll_seconds)),
        lease_seconds=max(10, int(args.lease_seconds)),
        run_once=bool(args.once),
    )


def resolve_runtime_mode(config: RunnerConfig) -> str:
    if config.runtime_mode == "auto":
        return "current-thread" if config.codex_thread_id else "cli"
    return config.runtime_mode


def build_runtime(*, config: RunnerConfig, default_workdir: str | None) -> RemoteExecutorRuntime:
    runtime_mode = resolve_runtime_mode(config)
    runtime_cwd = config.workdir or default_workdir
    if runtime_mode == "current-thread":
        return CodexCurrentThreadRemoteExecutorRuntime.from_env(
            cwd=runtime_cwd,
            thread_id=config.codex_thread_id,
        )
    return CodexCliRemoteExecutorRuntime.from_env(cwd=runtime_cwd)


def build_bridge(config: RunnerConfig) -> BridgeRemoteExecutorClient:
    load_dotenv(config.project_file.parents[2] / ".env")
    project = load_project(config.project_file)
    auth_token = os.environ[project.bridge.auth_token_env]
    bridge_client = BridgeClient(
        base_url=project.bridge.base_url,
        auth_token=auth_token,
    )
    return BridgeRemoteExecutorClient(bridge_client=bridge_client)


def build_live_control_client(*, cwd: str | None) -> CodexAppServerProcessClient:
    executable = _runtime_env_var(
        "REMOTE_EXECUTOR_CODEX_EXECUTABLE",
        "CHAT_PARTICIPANT_CODEX_EXECUTABLE",
        "CODEX_EXECUTABLE",
        default="codex",
    ) or "codex"
    config = CodexCurrentThreadRuntimeConfig(
        executable=_resolve_executable(executable),
        runtime_args=["app-server"],
        cwd=cwd,
        thread_id=None,
    )
    return CodexAppServerProcessClient(config=config)


def build_device_agent(
    *,
    config: RunnerConfig,
    runtime: RemoteExecutorRuntime,
    bridge: BridgeRemoteExecutorClient,
    default_workdir: str | None,
) -> RemoteCodexDeviceAgent:
    live_control_client: CodexAppServerProcessClient | None
    if isinstance(runtime, CodexCurrentThreadRemoteExecutorRuntime):
        live_control_client = runtime.client  # type: ignore[assignment]
    else:
        live_control_client = build_live_control_client(cwd=config.workdir or default_workdir)

    backend = LocalCodexBackend(
        machine_id=config.machine_id,
        display_name=os.getenv("REMOTE_EXECUTOR_MACHINE_DISPLAY_NAME") or socket.gethostname(),
        app_server_client=live_control_client,
        runtime_descriptor={
            "runtimeMode": "standalone-app-server",
            "runtimeBin": getattr(getattr(live_control_client, "config", None), "executable", None),
            "runtimeArgs": list(getattr(getattr(live_control_client, "config", None), "runtime_args", ["app-server"])),
            "cwd": getattr(getattr(live_control_client, "config", None), "cwd", config.workdir or default_workdir),
        },
    )
    worker_id = compact_text(
        os.getenv("REMOTE_EXECUTOR_WORKER_ID"),
        f"{config.machine_id}-remote-codex-agent",
    )
    return RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id=config.machine_id,
        display_name=backend.display_name,
        worker_id=worker_id,
    )


def build_session(config: RunnerConfig) -> RunnerSession:
    project = load_project(config.project_file)
    bridge = build_bridge(config)
    runtime = build_runtime(config=config, default_workdir=project.default_workdir)
    device_agent = build_device_agent(
        config=config,
        runtime=runtime,
        bridge=bridge,
        default_workdir=project.default_workdir,
    )
    return RunnerSession(
        bridge=bridge,
        runtime=runtime,
        device_agent=device_agent,
    )


def _add_activity_evidence(
    bridge: BridgeRemoteExecutorClient,
    *,
    task_id: str,
    actor_id: str,
    summary: str,
    activity,
) -> None:
    if not activity:
        return
    if activity.command_execution_count > 0:
        bridge.add_remote_task_evidence(
            task_id=task_id,
            actor_id=actor_id,
            kind="command_execution",
            summary=f"Executed {activity.command_execution_count} local command(s).",
            payload={"item_types": list(activity.item_types)},
        )
    if activity.read_command_count > 0:
        bridge.add_remote_task_evidence(
            task_id=task_id,
            actor_id=actor_id,
            kind="file_read",
            summary=f"Read from the workspace using {activity.read_command_count} command(s).",
            payload={"count": activity.read_command_count},
        )
    if activity.write_command_count > 0:
        bridge.add_remote_task_evidence(
            task_id=task_id,
            actor_id=actor_id,
            kind="file_write",
            summary=f"Modified the workspace using {activity.write_command_count} command(s).",
            payload={"count": activity.write_command_count},
        )
    if activity.test_command_count > 0:
        bridge.add_remote_task_evidence(
            task_id=task_id,
            actor_id=actor_id,
            kind="test_result",
            summary=f"Ran {activity.test_command_count} test command(s).",
            payload={"count": activity.test_command_count},
        )
    bridge.add_remote_task_note(
        task_id=task_id,
        actor_id=actor_id,
        kind="summary",
        content=summary,
    )


def _execute_claimed_task(
    *,
    bridge: BridgeRemoteExecutorClient,
    runtime: RemoteExecutorRuntime,
    claim: dict[str, object],
    actor_id: str,
    lease_seconds: int,
) -> bool:
    assignment = claim.get("current_assignment") or {}
    lease_token = str(assignment.get("lease_token") or "")
    if not lease_token:
        raise RuntimeError(f"Remote task {claim['id']} did not return a lease token after claim.")

    task_id = str(claim["id"])
    bridge.heartbeat_remote_task(
        task_id=task_id,
        actor_id=actor_id,
        lease_token=lease_token,
        phase="claimed",
        summary="Remote executor claimed the task and is preparing local Codex execution.",
        lease_seconds=lease_seconds,
    )
    try:
        result = runtime.execute_task(
            ExecutionTaskContext(
                task_id=task_id,
                machine_id=str(claim["machine_id"]),
                thread_id=str(claim["thread_id"]),
                objective=str(claim["objective"]),
                success_criteria=dict(claim.get("success_criteria") or {}),
                priority=str(claim.get("priority") or "normal"),
                origin_surface=str(claim.get("origin_surface") or "browser"),
                owner_actor_id=str(claim.get("owner_actor_id") or actor_id),
            ),
        )
        if result is None or not str(result.summary or "").strip():
            raise RuntimeError("Local Codex runtime returned no task summary.")

        activity = result.activity
        if activity is None or not activity.has_work_signal:
            raise RuntimeError(
                "Local Codex runtime returned without concrete work evidence. "
                "Remote executor expects command or tool activity before completing a task.",
            )

        _add_activity_evidence(
            bridge,
            task_id=task_id,
            actor_id=actor_id,
            summary=result.summary,
            activity=activity,
        )
        bridge.heartbeat_remote_task(
            task_id=task_id,
            actor_id=actor_id,
            lease_token=lease_token,
            phase="executing",
            summary=result.summary,
            lease_seconds=lease_seconds,
            commands_run_count=activity.command_execution_count,
            files_read_count=activity.read_command_count,
            files_modified_count=activity.write_command_count,
            tests_run_count=activity.test_command_count,
        )
        bridge.complete_remote_task(
            task_id=task_id,
            actor_id=actor_id,
            lease_token=lease_token,
            summary=result.summary,
        )
        LOGGER.info("Remote executor completed task %s", task_id)
        return True
    except Exception as exc:
        error_text = str(exc).strip() or exc.__class__.__name__
        bridge.fail_remote_task(
            task_id=task_id,
            actor_id=actor_id,
            lease_token=lease_token,
            error_text=error_text,
        )
        LOGGER.exception("Remote executor failed task %s: %s", task_id, error_text)
        return False


def run_cycle(session: RunnerSession, config: RunnerConfig) -> bool:
    activity = session.device_agent.poll_once()

    claim = session.bridge.claim_next_remote_task_for_machine(
        machine_id=config.machine_id,
        actor_id=config.actor_id,
        lease_seconds=config.lease_seconds,
    )
    if not claim:
        if not activity:
            LOGGER.info("No queued remote tasks or browser commands for machine %s", config.machine_id)
        return activity

    worked = _execute_claimed_task(
        bridge=session.bridge,
        runtime=session.runtime,
        claim=claim,
        actor_id=config.actor_id,
        lease_seconds=config.lease_seconds,
    )
    session.device_agent.mark_thread_dirty(str(claim.get("thread_id") or ""))
    session.device_agent.perform_sync(force=True)
    return worked or activity


def run_once(config: RunnerConfig) -> bool:
    session = build_session(config)
    return run_cycle(session, config)


def run_forever(config: RunnerConfig) -> None:
    session = build_session(config)
    while True:
        worked = run_cycle(session, config)
        if config.run_once:
            return
        if not worked:
            time.sleep(config.poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = parse_args(argv)
    run_forever(config)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual runner entrypoint
    raise SystemExit(main())
