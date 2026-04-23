from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .config_loader import load_project


REPO_ROOT = Path(__file__).resolve().parents[1]
BEHAVIORS_ROOT = REPO_ROOT / "pc_launcher" / "behaviors"
DEFAULT_GUILD_ID = "1494848853441253416"
DEFAULT_PARENT_CHANNEL_ID = "1494851246480298125"
DEFAULT_ALLOWED_USER_IDS = ["573769589318615040"]


class BehaviorEnvVar(BaseModel):
    name: str
    required: bool = False
    scope: str = "client"
    description: str = ""


class BehaviorRuntimeConfig(BaseModel):
    runner_module: str
    sender_module: str | None = None
    default_project_path: str
    project_profile_kind: str = "chat-participant"
    run_kind: str = "chat-participant"
    default_runtime_mode: str = "current-thread"
    default_reconnect_seconds: float = 3.0
    healthcheck_path: str = "/api/health"


class BehaviorManifest(BaseModel):
    name: str
    display_name: str
    description: str
    server_required: bool = True
    client_required: bool = True
    targets: list[str] = Field(default_factory=lambda: ["client"])
    requirements_file: str = "pc_launcher/requirements.txt"
    runtime: BehaviorRuntimeConfig
    env: list[BehaviorEnvVar] = Field(default_factory=list)
    doctor_checks: list[str] = Field(default_factory=list)
    smoke_tests: list[str] = Field(default_factory=list)
    install_notes: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class InstallResult:
    manifest: BehaviorManifest
    project_file: Path
    env_file: Path
    created_project: bool
    created_env: bool
    requirements_installed: bool


@dataclass(slots=True)
class DoctorCheckResult:
    name: str
    status: str
    detail: str


def _normalize_behavior_name(name: str) -> str:
    return name.strip().replace("-", "_")


def behavior_manifest_path(name: str, *, repo_root: Path = REPO_ROOT) -> Path:
    folder = _normalize_behavior_name(name)
    return repo_root / "pc_launcher" / "behaviors" / folder / "behavior.yaml"


def load_behavior_manifest(name: str, *, repo_root: Path = REPO_ROOT) -> BehaviorManifest:
    manifest_path = behavior_manifest_path(name, repo_root=repo_root)
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return BehaviorManifest.model_validate(data)


def _sample_project_defaults(repo_root: Path) -> dict[str, Any]:
    sample_project = repo_root / "pc_launcher" / "projects" / "sample" / "project.yaml"
    if sample_project.exists():
        loaded = load_project(sample_project)
        return {
            "guild_id": loaded.guild_id,
            "parent_channel_id": loaded.parent_channel_id,
            "allowed_user_ids": loaded.allowed_user_ids,
            "discord": loaded.discord.model_dump(mode="json"),
        }
    return {
        "guild_id": DEFAULT_GUILD_ID,
        "parent_channel_id": DEFAULT_PARENT_CHANNEL_ID,
        "allowed_user_ids": list(DEFAULT_ALLOWED_USER_IDS),
        "discord": {
            "thread_name_template": "[{project_name}] {timestamp}",
            "auto_archive_duration": 1440,
        },
    }


def _chat_participant_project_payload(
    *,
    behavior_name: str,
    bridge_url: str,
    bridge_token_env: str,
    workdir: str,
    repo_root: Path,
) -> dict[str, Any]:
    defaults = _sample_project_defaults(repo_root)
    return {
        "profile_name": behavior_name,
        "default_target_name": behavior_name,
        "default_workdir": workdir,
        "guild_id": defaults["guild_id"],
        "parent_channel_id": defaults["parent_channel_id"],
        "allowed_user_ids": defaults["allowed_user_ids"],
        "discord": defaults["discord"],
        "bridge": {
            "base_url": bridge_url,
            "auth_token_env": bridge_token_env,
        },
        "agents": [],
        "startup": {
            "send_ready_message": False,
            "restore_last_session": False,
            "open_tools": [],
        },
        "artifacts": {
            "sessions_dir": "_discord_sessions",
            "quiet_discord": True,
        },
        "policy": {
            "max_parallel_agents": 1,
            "auto_retry": True,
            "max_retries": 1,
            "quiet_discord": True,
            "approval_mode": "critical_only",
            "allow_cross_agent_handoff": False,
        },
        "verification": {
            "enabled": False,
        },
    }


def _remote_executor_project_payload(
    *,
    behavior_name: str,
    bridge_url: str,
    bridge_token_env: str,
    workdir: str,
    repo_root: Path,
) -> dict[str, Any]:
    defaults = _sample_project_defaults(repo_root)
    return {
        "profile_name": behavior_name,
        "default_target_name": behavior_name,
        "default_workdir": workdir,
        "guild_id": defaults["guild_id"],
        "parent_channel_id": defaults["parent_channel_id"],
        "allowed_user_ids": defaults["allowed_user_ids"],
        "discord": defaults["discord"],
        "bridge": {
            "base_url": bridge_url,
            "auth_token_env": bridge_token_env,
        },
        "agents": [],
        "startup": {
            "send_ready_message": False,
            "restore_last_session": False,
            "open_tools": [],
        },
        "artifacts": {
            "sessions_dir": "_discord_sessions",
            "quiet_discord": True,
        },
        "policy": {
            "max_parallel_agents": 1,
            "auto_retry": True,
            "max_retries": 1,
            "quiet_discord": True,
            "approval_mode": "critical_only",
            "allow_cross_agent_handoff": False,
        },
        "verification": {
            "enabled": False,
        },
    }


def _build_project_payload(
    manifest: BehaviorManifest,
    *,
    bridge_url: str,
    bridge_token_env: str,
    workdir: str,
    repo_root: Path,
) -> dict[str, Any]:
    kind = manifest.runtime.project_profile_kind
    if kind == "chat-participant":
        return _chat_participant_project_payload(
            behavior_name=manifest.name,
            bridge_url=bridge_url,
            bridge_token_env=bridge_token_env,
            workdir=workdir,
            repo_root=repo_root,
        )
    if kind == "remote-executor":
        return _remote_executor_project_payload(
            behavior_name=manifest.name,
            bridge_url=bridge_url,
            bridge_token_env=bridge_token_env,
            workdir=workdir,
            repo_root=repo_root,
        )
    raise ValueError(f"Unsupported behavior project profile kind: {kind}")


def install_behavior(
    name: str,
    *,
    repo_root: Path = REPO_ROOT,
    project_file: Path | None = None,
    env_file: Path | None = None,
    bridge_url: str | None = None,
    bridge_token_env: str = "BRIDGE_TOKEN",
    workdir: str | None = None,
    force: bool = False,
    install_requirements: bool = True,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> InstallResult:
    manifest = load_behavior_manifest(name, repo_root=repo_root)
    command_runner = command_runner or subprocess.run

    requirements_file = repo_root / manifest.requirements_file
    resolved_project_file = project_file or (repo_root / manifest.runtime.default_project_path)
    resolved_env_file = env_file or (repo_root / "pc_launcher" / ".env")
    resolved_bridge_url = bridge_url or "http://172.30.1.12:18080"
    resolved_workdir = workdir or str(repo_root)

    created_env = False
    env_example = repo_root / "pc_launcher" / ".env.example"
    resolved_env_file.parent.mkdir(parents=True, exist_ok=True)
    if force or not resolved_env_file.exists():
        if env_example.exists():
            shutil.copyfile(env_example, resolved_env_file)
        else:
            resolved_env_file.write_text("", encoding="utf-8")
        created_env = True

    created_project = False
    resolved_project_file.parent.mkdir(parents=True, exist_ok=True)
    if force or not resolved_project_file.exists():
        payload = _build_project_payload(
            manifest,
            bridge_url=resolved_bridge_url,
            bridge_token_env=bridge_token_env,
            workdir=resolved_workdir,
            repo_root=repo_root,
        )
        resolved_project_file.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        created_project = True

    requirements_installed = False
    if install_requirements:
        command_runner(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements_file)],
            cwd=str(repo_root),
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        requirements_installed = True

    return InstallResult(
        manifest=manifest,
        project_file=resolved_project_file,
        env_file=resolved_env_file,
        created_project=created_project,
        created_env=created_env,
        requirements_installed=requirements_installed,
    )


def _probe_command(
    command: list[str],
    *,
    cwd: Path,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[bool, str]:
    try:
        completed = command_runner(
            command,
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # pragma: no cover - defensive probe wrapper
        return False, str(exc)
    output = (completed.stdout or completed.stderr or "").strip()
    return True, output or "ok"


def doctor_behavior(
    name: str,
    *,
    repo_root: Path = REPO_ROOT,
    project_file: Path | None = None,
    env_file: Path | None = None,
    codex_executable: str | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    http_get: Callable[..., requests.Response] | None = None,
) -> list[DoctorCheckResult]:
    manifest = load_behavior_manifest(name, repo_root=repo_root)
    resolved_project_file = project_file or (repo_root / manifest.runtime.default_project_path)
    resolved_env_file = env_file or (repo_root / "pc_launcher" / ".env")
    command_runner = command_runner or subprocess.run
    http_get = http_get or requests.get

    results: list[DoctorCheckResult] = []

    if resolved_env_file.exists():
        results.append(DoctorCheckResult(name="env_file", status="pass", detail=str(resolved_env_file)))
    else:
        results.append(DoctorCheckResult(name="env_file", status="warn", detail=f"Missing {resolved_env_file}"))

    if not resolved_project_file.exists():
        results.append(
            DoctorCheckResult(name="project_file", status="fail", detail=f"Missing {resolved_project_file}"),
        )
        return results

    try:
        project = load_project(resolved_project_file)
    except Exception as exc:
        results.append(DoctorCheckResult(name="project_file", status="fail", detail=str(exc)))
        return results

    results.append(DoctorCheckResult(name="project_file", status="pass", detail=str(resolved_project_file)))

    load_dotenv(resolved_env_file)
    token_value = os.getenv(project.bridge.auth_token_env)
    if token_value:
        results.append(
            DoctorCheckResult(
                name="auth_token",
                status="pass",
                detail=f"{project.bridge.auth_token_env} is set",
            ),
        )
    else:
        results.append(
            DoctorCheckResult(
                name="auth_token",
                status="fail",
                detail=f"Set {project.bridge.auth_token_env} in {resolved_env_file}",
            ),
        )

    health_url = f"{project.bridge.base_url.rstrip('/')}{manifest.runtime.healthcheck_path}"
    try:
        response = http_get(health_url, timeout=10)
        if response.ok:
            results.append(DoctorCheckResult(name="bridge_health", status="pass", detail=health_url))
        else:
            results.append(
                DoctorCheckResult(
                    name="bridge_health",
                    status="fail",
                    detail=f"{health_url} -> {response.status_code}",
                ),
            )
    except Exception as exc:  # pragma: no cover - network dependent
        results.append(DoctorCheckResult(name="bridge_health", status="fail", detail=str(exc)))

    executable = codex_executable or os.getenv("CHAT_PARTICIPANT_CODEX_EXECUTABLE") or os.getenv("CODEX_EXECUTABLE") or "codex"
    ok, detail = _probe_command([executable, "--help"], cwd=repo_root, command_runner=command_runner)
    results.append(
        DoctorCheckResult(
            name="codex_help",
            status="pass" if ok else "fail",
            detail=detail,
        ),
    )
    ok, detail = _probe_command([executable, "app-server", "--help"], cwd=repo_root, command_runner=command_runner)
    results.append(
        DoctorCheckResult(
            name="codex_app_server_help",
            status="pass" if ok else "fail",
            detail=detail,
        ),
    )
    return results


def build_behavior_run_command(
    name: str,
    *,
    repo_root: Path = REPO_ROOT,
    project_file: Path | None = None,
    thread_id: str | None = None,
    actor_name: str | None = None,
    machine_id: str | None = None,
    actor_id: str | None = None,
    runtime_mode: str | None = None,
    codex_thread_id: str | None = None,
    poll_seconds: float | None = None,
    lease_seconds: int | None = None,
    allow_unprompted: bool = False,
    run_once: bool = False,
) -> list[str]:
    manifest = load_behavior_manifest(name, repo_root=repo_root)
    resolved_project_file = project_file or (repo_root / manifest.runtime.default_project_path)
    run_kind = manifest.runtime.run_kind

    if run_kind == "chat-participant":
        if not thread_id:
            raise ValueError("chat-participant requires thread_id.")
        if not actor_name:
            raise ValueError("chat-participant requires actor_name.")
        command = [
            sys.executable,
            "-m",
            manifest.runtime.runner_module,
            "--project-file",
            str(resolved_project_file),
            "--thread-id",
            thread_id,
            "--actor-name",
            actor_name,
            "--runtime-mode",
            runtime_mode or manifest.runtime.default_runtime_mode,
            "--poll-seconds",
            str(poll_seconds or manifest.runtime.default_reconnect_seconds),
        ]
        if codex_thread_id:
            command.extend(["--codex-thread-id", codex_thread_id])
        if allow_unprompted:
            command.append("--allow-unprompted")
        if run_once:
            command.append("--once")
        return command

    if run_kind == "remote-executor":
        if not machine_id:
            raise ValueError("remote-executor requires machine_id.")
        resolved_actor_id = actor_id or actor_name
        if not resolved_actor_id:
            raise ValueError("remote-executor requires actor_id.")
        command = [
            sys.executable,
            "-m",
            manifest.runtime.runner_module,
            "--project-file",
            str(resolved_project_file),
            "--machine-id",
            machine_id,
            "--actor-id",
            resolved_actor_id,
            "--runtime-mode",
            runtime_mode or manifest.runtime.default_runtime_mode,
            "--poll-seconds",
            str(poll_seconds or manifest.runtime.default_reconnect_seconds),
            "--lease-seconds",
            str(lease_seconds or 90),
        ]
        if codex_thread_id:
            command.extend(["--codex-thread-id", codex_thread_id])
        if run_once:
            command.append("--once")
        return command

    raise ValueError(f"Unsupported behavior run kind: {run_kind}")


def build_behavior_send_command(
    name: str,
    *,
    repo_root: Path = REPO_ROOT,
    project_file: Path | None = None,
    thread_id: str,
    actor_name: str,
    actor_kind: str = "ai",
    message: str | None = None,
    message_file: Path | None = None,
    read_stdin: bool = False,
) -> list[str]:
    manifest = load_behavior_manifest(name, repo_root=repo_root)
    if not manifest.runtime.sender_module:
        raise ValueError(f"Behavior '{name}' does not define a sender module.")
    resolved_project_file = project_file or (repo_root / manifest.runtime.default_project_path)
    command = [
        sys.executable,
        "-m",
        manifest.runtime.sender_module,
        "--project-file",
        str(resolved_project_file),
        "--thread-id",
        thread_id,
        "--actor-name",
        actor_name,
        "--actor-kind",
        actor_kind,
    ]
    if message is not None:
        command.extend(["--message", message])
    elif message_file is not None:
        command.extend(["--message-file", str(message_file)])
    elif read_stdin:
        command.append("--stdin")
    else:
        raise ValueError("Provide --message, --message-file, or --stdin.")
    return command


def _print_install_result(result: InstallResult) -> None:
    print(f"Behavior: {result.manifest.display_name} ({result.manifest.name})")
    print(f"Project file: {result.project_file}")
    print(f"Env file: {result.env_file}")
    print(f"Created project file: {'yes' if result.created_project else 'no'}")
    print(f"Created env file: {'yes' if result.created_env else 'no'}")
    print(f"Installed requirements: {'yes' if result.requirements_installed else 'no'}")
    for note in result.manifest.install_notes:
        print(f"Note: {note}")


def _print_doctor_results(results: list[DoctorCheckResult]) -> None:
    for result in results:
        print(f"[{result.status.upper()}] {result.name}: {result.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Installable behavior tools for Opscure runtimes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="Install or scaffold a behavior on this PC.")
    install_parser.add_argument("behavior", help="Behavior name, for example chat-participant.")
    install_parser.add_argument("--project-file", help="Override the generated project.yaml path.")
    install_parser.add_argument("--env-file", help="Override the .env path.")
    install_parser.add_argument("--bridge-url", help="Bridge base URL for the generated project file.")
    install_parser.add_argument("--bridge-token-env", default="BRIDGE_TOKEN", help="Token env name.")
    install_parser.add_argument("--workdir", help="Default workdir written into the generated project file.")
    install_parser.add_argument("--force", action="store_true", help="Overwrite existing files.")
    install_parser.add_argument("--skip-pip", action="store_true", help="Skip dependency installation.")

    doctor_parser = subparsers.add_parser("doctor", help="Run health checks for a behavior install.")
    doctor_parser.add_argument("behavior", help="Behavior name, for example chat-participant.")
    doctor_parser.add_argument("--project-file", help="Behavior project.yaml path.")
    doctor_parser.add_argument("--env-file", help="Behavior .env path.")
    doctor_parser.add_argument("--codex-executable", help="Codex executable override.")

    run_parser = subparsers.add_parser("run", help="Run a behavior entrypoint.")
    run_parser.add_argument("behavior", help="Behavior name, for example chat-participant.")
    run_parser.add_argument("--project-file", help="Behavior project.yaml path.")
    run_parser.add_argument("--thread-id", help="Target chat thread id for chat-oriented behaviors.")
    run_parser.add_argument("--actor-name", help="Participant actor name for chat-oriented behaviors.")
    run_parser.add_argument("--machine-id", help="Target machine id for task-oriented behaviors.")
    run_parser.add_argument("--actor-id", help="Executor actor id for task-oriented behaviors.")
    run_parser.add_argument("--runtime-mode", help="Runtime mode override.")
    run_parser.add_argument("--codex-thread-id", help="Current Codex desktop thread id.")
    run_parser.add_argument("--poll-seconds", type=float, help="Reconnect backoff seconds.")
    run_parser.add_argument("--lease-seconds", type=int, help="Lease duration for task-oriented behaviors.")
    run_parser.add_argument("--allow-unprompted", action="store_true", help="Allow non-targeted replies.")
    run_parser.add_argument("--once", action="store_true", help="Run one sync cycle and exit.")

    send_parser = subparsers.add_parser("send", help="Send a behavior message using the behavior sender.")
    send_parser.add_argument("behavior", help="Behavior name, for example chat-participant.")
    send_parser.add_argument("--project-file", help="Behavior project.yaml path.")
    send_parser.add_argument("--thread-id", required=True, help="Target chat thread id.")
    send_parser.add_argument("--actor-name", required=True, help="Participant actor name.")
    send_parser.add_argument("--actor-kind", default="ai", help="Actor kind.")
    source_group = send_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--message", help="Inline message text.")
    source_group.add_argument("--message-file", help="UTF-8 message file path.")
    source_group.add_argument("--stdin", action="store_true", help="Read UTF-8 message from stdin.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "install":
        result = install_behavior(
            args.behavior,
            project_file=Path(args.project_file).resolve() if args.project_file else None,
            env_file=Path(args.env_file).resolve() if args.env_file else None,
            bridge_url=args.bridge_url,
            bridge_token_env=args.bridge_token_env,
            workdir=args.workdir,
            force=bool(args.force),
            install_requirements=not bool(args.skip_pip),
        )
        _print_install_result(result)
        return 0

    if args.command == "doctor":
        results = doctor_behavior(
            args.behavior,
            project_file=Path(args.project_file).resolve() if args.project_file else None,
            env_file=Path(args.env_file).resolve() if args.env_file else None,
            codex_executable=args.codex_executable,
        )
        _print_doctor_results(results)
        return 1 if any(result.status == "fail" for result in results) else 0

    if args.command == "run":
        command = build_behavior_run_command(
            args.behavior,
            project_file=Path(args.project_file).resolve() if args.project_file else None,
            thread_id=args.thread_id,
            actor_name=args.actor_name,
            machine_id=args.machine_id,
            actor_id=args.actor_id,
            runtime_mode=args.runtime_mode,
            codex_thread_id=args.codex_thread_id,
            poll_seconds=args.poll_seconds,
            lease_seconds=args.lease_seconds,
            allow_unprompted=bool(args.allow_unprompted),
            run_once=bool(args.once),
        )
        completed = subprocess.run(command, cwd=str(REPO_ROOT))
        return int(completed.returncode)

    if args.command == "send":
        command = build_behavior_send_command(
            args.behavior,
            project_file=Path(args.project_file).resolve() if args.project_file else None,
            thread_id=args.thread_id,
            actor_name=args.actor_name,
            actor_kind=args.actor_kind,
            message=args.message,
            message_file=Path(args.message_file).resolve() if args.message_file else None,
            read_stdin=bool(args.stdin),
        )
        completed = subprocess.run(command, cwd=str(REPO_ROOT))
        return int(completed.returncode)

    raise RuntimeError(f"Unsupported command {args.command}")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
