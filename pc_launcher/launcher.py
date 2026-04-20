from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

try:
    from .bridge_client import BridgeClient, BridgeClientError
    from .config_loader import ProjectConfig, discover_projects
    from .project_finder import ProjectFinder
    from .verification_runner import CommandVerificationRunner
except ImportError:  # pragma: no cover - script mode support
    from bridge_client import BridgeClient, BridgeClientError
    from config_loader import ProjectConfig, discover_projects
    from project_finder import ProjectFinder
    from verification_runner import CommandVerificationRunner

LOGGER = logging.getLogger(__name__)
ARG_PATTERN_TEMPLATE = r"--{name}\s+(?P<quote>[\"']?)(?P<value>.+?)(?P=quote)(?:\s+--|\s*$)"


@dataclass(slots=True)
class ManagedWorker:
    process: subprocess.Popen[str]
    session_id: str
    agent_name: str
    worker_id: str
    project_file: Path


@dataclass(slots=True)
class ExternalWorkerProcess:
    pid: int
    session_id: str
    agent_name: str
    worker_id: str
    project_file: Path
    command_line: str


class LauncherInstanceLock(AbstractContextManager["LauncherInstanceLock"]):
    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path).resolve()
        self._handle = None

    def __enter__(self) -> "LauncherInstanceLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:  # pragma: no cover - platform specific
            self._handle.close()
            self._handle = None
            raise RuntimeError(
                f"Launcher instance lock is already held at {self.lock_path}. "
                "Another launcher process is probably already running."
            ) from exc

        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n".encode("utf-8"))
        self._handle.flush()
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class LauncherDaemon:
    def __init__(
        self,
        *,
        projects_dir: str | Path,
        launcher_id: str,
        poll_interval_seconds: int = 5,
        catalog_refresh_seconds: int = 30,
        claim_capacity: int = 10,
        find_capacity: int = 1,
        verify_capacity: int = 1,
    ) -> None:
        load_dotenv(Path(__file__).resolve().with_name(".env"))
        self.projects_dir = Path(projects_dir).resolve()
        self.launcher_id = launcher_id
        self.poll_interval_seconds = poll_interval_seconds
        self.catalog_refresh_seconds = catalog_refresh_seconds
        self.claim_capacity = claim_capacity
        self.find_capacity = find_capacity
        self.verify_capacity = verify_capacity
        self.hostname = socket.gethostname()
        self._managed_workers: dict[tuple[str, str], ManagedWorker] = {}
        self._project_index: dict[str, tuple[Path, ProjectConfig]] = {}
        self._bridge_client: BridgeClient | None = None
        self._last_catalog_push_at = 0.0
        self._verification_runner = CommandVerificationRunner()
        self._refresh_projects()

    def run_forever(self) -> None:
        LOGGER.info("Launcher %s scanning projects under %s", self.launcher_id, self.projects_dir)
        self._reconcile_external_workers()
        while True:
            try:
                self._refresh_projects()
                self._register_catalog_if_needed(force=False)
                self._reap_workers()
                self._reconcile_external_workers()
                self._reconcile_managed_workers_with_bridge()
                self._claim_and_launch()
                self._claim_and_resolve_project_finds()
                self._claim_and_run_verifications()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Launcher loop failed: %s", exc)
            time.sleep(self.poll_interval_seconds)

    def _refresh_projects(self) -> None:
        self._project_index = {
            config.profile_name: (project_file, config)
            for project_file, config in discover_projects(self.projects_dir)
        }
        if not self._project_index:
            raise RuntimeError(f"No project.yaml files found under {self.projects_dir}.")
        first_project = next(iter(self._project_index.values()))[1]
        self._bridge_client = BridgeClient(
            base_url=first_project.bridge.base_url,
            auth_token=os.environ[first_project.bridge.auth_token_env],
        )

    def _register_catalog_if_needed(self, *, force: bool) -> None:
        assert self._bridge_client is not None
        now = time.time()
        if not force and now - self._last_catalog_push_at < self.catalog_refresh_seconds:
            return

        manifests = [config.to_bridge_manifest() for _, config in self._project_index.values()]
        self._bridge_client.register_projects(
            launcher_id=self.launcher_id,
            hostname=self.hostname,
            projects=manifests,
        )
        self._last_catalog_push_at = now
        LOGGER.info("Registered %s project manifests", len(manifests))

    def _claim_and_launch(self) -> None:
        assert self._bridge_client is not None
        launches = self._bridge_client.claim_launches(
            launcher_id=self.launcher_id,
            capacity=self.claim_capacity,
        )
        for launch in launches:
            self._ensure_session_workers(launch)

    def _claim_and_resolve_project_finds(self) -> None:
        assert self._bridge_client is not None
        find_requests = self._bridge_client.claim_project_finds(
            launcher_id=self.launcher_id,
            capacity=self.find_capacity,
        )
        for request in find_requests:
            self._resolve_project_find(request)

    def _claim_and_run_verifications(self) -> None:
        assert self._bridge_client is not None
        verify_runs = self._bridge_client.claim_verification_runs(
            launcher_id=self.launcher_id,
            capacity=self.verify_capacity,
        )
        for run in verify_runs:
            self._execute_verification_run(run)

    def _ensure_session_workers(self, launch: dict[str, object]) -> None:
        project_name = str(launch["project_name"])
        preset_name = str(launch.get("preset") or project_name)
        session_id = str(launch["session_id"])
        workdir_override = str(launch.get("workdir") or "").strip() or None
        if preset_name not in self._project_index:
            LOGGER.warning("Bridge requested unknown preset %s for session %s", preset_name, project_name)
            return

        project_file, project = self._project_index[preset_name]
        for agent in project.agents:
            key = (session_id, agent.name)
            managed = self._managed_workers.get(key)
            if managed is not None and managed.process.poll() is None:
                continue
            self._managed_workers[key] = self._spawn_worker(
                project_file=project_file,
                session_id=session_id,
                agent_name=agent.name,
                workdir_override=workdir_override,
            )

    def _spawn_worker(
        self,
        *,
        project_file: Path,
        session_id: str,
        agent_name: str,
        workdir_override: str | None,
    ) -> ManagedWorker:
        worker_script = Path(__file__).resolve().with_name("cli_worker.py")
        worker_id = str(uuid.uuid4())
        command = [
            sys.executable,
            str(worker_script),
            "--project-file",
            str(project_file),
            "--session-id",
            session_id,
            "--agent-name",
            agent_name,
            "--launcher-id",
            self.launcher_id,
            "--worker-id",
            worker_id,
        ]
        if workdir_override:
            command.extend(["--workdir-override", workdir_override])
        LOGGER.info("Spawning worker session=%s agent=%s", session_id, agent_name)
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parent, text=True)
        return ManagedWorker(
            process=process,
            session_id=session_id,
            agent_name=agent_name,
            worker_id=worker_id,
            project_file=project_file,
        )

    def _resolve_project_find(self, request: dict[str, object]) -> None:
        assert self._bridge_client is not None
        find_id = str(request["id"])
        preset_name = str(request["preset"])
        if preset_name not in self._project_index:
            self._bridge_client.complete_project_find(
                find_id=find_id,
                launcher_id=self.launcher_id,
                status="failed",
                selected_path=None,
                selected_name=None,
                reason=None,
                confidence=None,
                candidates=[],
                error_text=f"Unknown preset `{preset_name}` on launcher `{self.launcher_id}`.",
            )
            return

        project_file, project = self._project_index[preset_name]
        finder = ProjectFinder(project_file=project_file, project=project)
        query_text = str(request.get("query_text") or "").strip()
        try:
            result = finder.find(query_text)
            self._bridge_client.complete_project_find(
                find_id=find_id,
                launcher_id=self.launcher_id,
                status=str(result.get("status") or "failed"),
                selected_path=result.get("selected_path"),
                selected_name=result.get("selected_name"),
                reason=result.get("reason"),
                confidence=result.get("confidence"),
                candidates=list(result.get("candidates") or []),
                error_text=None,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Project find failed for preset=%s query=%s", preset_name, query_text)
            self._bridge_client.complete_project_find(
                find_id=find_id,
                launcher_id=self.launcher_id,
                status="failed",
                selected_path=None,
                selected_name=None,
                reason=None,
                confidence=None,
                candidates=[],
                error_text=str(exc),
            )

    def _execute_verification_run(self, run: dict[str, object]) -> None:
        assert self._bridge_client is not None
        profile_name = str(run["profile_name"])
        if profile_name not in self._project_index:
            self._bridge_client.complete_verification_run(
                run_id=str(run["id"]),
                launcher_id=self.launcher_id,
                status="failed",
                summary_text=None,
                error_text=f"Unknown profile `{profile_name}` on launcher `{self.launcher_id}`.",
                artifacts=[],
            )
            return

        _, project = self._project_index[profile_name]
        try:
            result = self._verification_runner.run(
                run_payload=run,
                project=project,
            )
            self._bridge_client.complete_verification_run(
                run_id=str(run["id"]),
                launcher_id=self.launcher_id,
                status=result.status,
                summary_text=result.summary_text,
                error_text=result.error_text,
                artifacts=result.artifacts,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Verification run %s failed unexpectedly", run.get("id"))
            self._bridge_client.complete_verification_run(
                run_id=str(run["id"]),
                launcher_id=self.launcher_id,
                status="failed",
                summary_text=None,
                error_text=str(exc),
                artifacts=[],
            )

    def _reap_workers(self) -> None:
        completed = []
        for key, managed in self._managed_workers.items():
            if managed.process.poll() is None:
                continue
            completed.append(key)
            LOGGER.warning(
                "Worker session=%s agent=%s exited with code %s",
                key[0],
                key[1],
                managed.process.returncode,
            )
        for key in completed:
            self._managed_workers.pop(key, None)

    def _reconcile_external_workers(self) -> None:
        managed_pids = {
            managed.process.pid
            for managed in self._managed_workers.values()
            if managed.process.poll() is None and managed.process.pid is not None
        }
        valid_project_files = {
            str(project_file.resolve()).lower()
            for project_file, _ in self._project_index.values()
        }
        for worker in self._list_launcher_worker_processes():
            if worker.pid in managed_pids:
                continue
            reason = "stale external worker was not managed by the active launcher daemon"
            if not worker.project_file.exists() or str(worker.project_file.resolve()).lower() not in valid_project_files:
                reason = "stale external worker uses an unknown or missing profile file"
            self._terminate_external_worker_process(worker, reason)

    def _reconcile_managed_workers_with_bridge(self) -> None:
        assert self._bridge_client is not None
        completed: list[tuple[str, str]] = []
        for key, managed in self._managed_workers.items():
            if managed.process.poll() is not None:
                completed.append(key)
                continue
            try:
                summary = self._bridge_client.get_session(managed.session_id)
            except BridgeClientError as exc:
                if "404" in str(exc):
                    self._terminate_managed_worker(managed, "bridge no longer knows this session")
                    completed.append(key)
                continue
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Managed worker reconciliation failed for %s/%s: %s", managed.session_id, managed.agent_name, exc)
                continue

            if str(summary.get("status") or "") in {"closed", "failed_start"} or str(summary.get("desired_status") or "") == "closed":
                self._terminate_managed_worker(managed, "session is no longer active")
                completed.append(key)
                continue

            agents = list(summary.get("agents") or [])
            matching_agent = next(
                (
                    agent
                    for agent in agents
                    if str(agent.get("agent_name") or "") == managed.agent_name
                ),
                None,
            )
            if matching_agent is None:
                self._terminate_managed_worker(managed, "session no longer contains this agent")
                completed.append(key)
                continue
            registered_worker_id = str(matching_agent.get("worker_id") or "").strip()
            if registered_worker_id and registered_worker_id != managed.worker_id:
                self._terminate_managed_worker(managed, "bridge reassigned this agent to a different worker")
                completed.append(key)

        for key in completed:
            self._managed_workers.pop(key, None)

    @staticmethod
    def _extract_arg(command_line: str, name: str) -> str | None:
        pattern = re.compile(ARG_PATTERN_TEMPLATE.format(name=re.escape(name)), re.IGNORECASE)
        match = pattern.search(command_line)
        if match is None:
            return None
        value = match.group("value").strip()
        if value.endswith('"') and not value.startswith('"'):
            value = value[:-1]
        if value.endswith("'") and not value.startswith("'"):
            value = value[:-1]
        return value

    def _list_launcher_worker_processes(self) -> list[ExternalWorkerProcess]:
        if os.name == "nt":
            return self._list_launcher_worker_processes_windows()
        return self._list_launcher_worker_processes_posix()

    def _list_launcher_worker_processes_windows(self) -> list[ExternalWorkerProcess]:
        command = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and $_.CommandLine -like '*cli_worker.py*' } | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        payload = json.loads(completed.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return self._parse_external_workers(rows)

    def _list_launcher_worker_processes_posix(self) -> list[ExternalWorkerProcess]:
        completed = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            return []
        rows = []
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            pid_text, _, command_line = stripped.partition(" ")
            if not pid_text.isdigit():
                continue
            rows.append({"ProcessId": int(pid_text), "CommandLine": command_line})
        return self._parse_external_workers(rows)

    def _parse_external_workers(self, rows: list[dict[str, object]]) -> list[ExternalWorkerProcess]:
        discovered: list[ExternalWorkerProcess] = []
        for row in rows:
            command_line = str(row.get("CommandLine") or "").strip()
            if "cli_worker.py" not in command_line:
                continue
            launcher_id = self._extract_arg(command_line, "launcher-id")
            if launcher_id != self.launcher_id:
                continue
            session_id = self._extract_arg(command_line, "session-id")
            agent_name = self._extract_arg(command_line, "agent-name")
            worker_id = self._extract_arg(command_line, "worker-id")
            project_file = self._extract_arg(command_line, "project-file")
            pid = int(row.get("ProcessId") or 0)
            if not (pid and session_id and agent_name and worker_id and project_file):
                continue
            discovered.append(
                ExternalWorkerProcess(
                    pid=pid,
                    session_id=session_id,
                    agent_name=agent_name,
                    worker_id=worker_id,
                    project_file=Path(project_file).resolve(),
                    command_line=command_line,
                ),
            )
        return discovered

    def _terminate_external_worker_process(self, worker: ExternalWorkerProcess, reason: str) -> None:
        LOGGER.warning(
            "Terminating external worker pid=%s session=%s agent=%s: %s",
            worker.pid,
            worker.session_id,
            worker.agent_name,
            reason,
        )
        self._terminate_pid(worker.pid)

    def _terminate_managed_worker(self, managed: ManagedWorker, reason: str) -> None:
        LOGGER.warning(
            "Terminating managed worker pid=%s session=%s agent=%s: %s",
            managed.process.pid,
            managed.session_id,
            managed.agent_name,
            reason,
        )
        if managed.process.poll() is not None:
            return
        managed.process.terminate()
        try:
            managed.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            managed.process.kill()

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        if pid <= 0:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return
        try:
            os.kill(pid, 15)
        except OSError:
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Windows launcher daemon.")
    parser.add_argument(
        "mode",
        choices=["daemon"],
        help="Launcher mode. The MVP uses a single daemon mode.",
    )
    parser.add_argument("--projects-dir", default=str(Path(__file__).resolve().with_name("projects")))
    parser.add_argument("--launcher-id", default=socket.gethostname())
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument("--catalog-refresh", type=int, default=30)
    parser.add_argument("--claim-capacity", type=int, default=10)
    parser.add_argument("--find-capacity", type=int, default=1)
    parser.add_argument("--verify-capacity", type=int, default=1)
    return parser


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    daemon = LauncherDaemon(
        projects_dir=args.projects_dir,
        launcher_id=args.launcher_id,
        poll_interval_seconds=args.poll_interval,
        catalog_refresh_seconds=args.catalog_refresh,
        claim_capacity=args.claim_capacity,
        find_capacity=args.find_capacity,
        verify_capacity=args.verify_capacity,
    )
    lock_path = Path(args.projects_dir).resolve() / f".ops-cure-launcher-{args.launcher_id}.lock"
    with LauncherInstanceLock(lock_path):
        daemon.run_forever()


if __name__ == "__main__":
    main()
