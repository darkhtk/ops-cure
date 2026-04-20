from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

try:
    from .artifact_workspace import BridgeCompletionPayload, SessionWorkspace
    from .bridge_client import BridgeClient, BridgeClientError
    from .cli_adapters import AdapterContext, get_adapter
    from .config_loader import AgentConfig, ProjectConfig, find_agent, load_project
    from .process_io import build_utf8_subprocess_env, text_subprocess_kwargs
except ImportError:  # pragma: no cover - script mode support
    from artifact_workspace import BridgeCompletionPayload, SessionWorkspace
    from bridge_client import BridgeClient, BridgeClientError
    from cli_adapters import AdapterContext, get_adapter
    from config_loader import AgentConfig, ProjectConfig, find_agent, load_project
    from process_io import build_utf8_subprocess_env, text_subprocess_kwargs

LOGGER = logging.getLogger(__name__)
HANDOFF_TIMEOUT_CAP_SECONDS = 900
ROUTING_TIMEOUT_CAP_SECONDS = 240
RESTART_TIMEOUT_CAP_SECONDS = 120


class WorkerRuntime:
    def __init__(
        self,
        *,
        project_file: str | Path,
        session_id: str,
        agent_name: str,
        launcher_id: str,
        workdir_override: str | None = None,
        worker_id: str | None = None,
        heartbeat_interval_seconds: int = 15,
        poll_interval_seconds: int = 3,
    ) -> None:
        load_dotenv(Path(__file__).resolve().with_name(".env"))
        self.project_file = Path(project_file).resolve()
        self.project: ProjectConfig = load_project(self.project_file)
        if workdir_override:
            self.project.workdir = str(Path(workdir_override).resolve())
        self.agent: AgentConfig = find_agent(self.project, agent_name)
        self.session_id = session_id
        self.agent_name = agent_name
        self.launcher_id = launcher_id
        self.worker_id = worker_id or str(uuid.uuid4())
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.bridge_client = BridgeClient(
            base_url=self.project.bridge.base_url,
            auth_token=os.environ[self.project.bridge.auth_token_env],
        )
        self.system_prompt = self.project.prompt_text_for(self.agent, self.project_file)
        self.adapter = get_adapter(self.agent.cli)
        self._status = "starting"
        self._status_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._current_process: subprocess.Popen[str] | None = None
        self._session_name = self.project.resolved_default_target_name
        self._session_preset = self.project.profile_name
        self._workspace: SessionWorkspace | None = None
        self._last_workspace_reconcile_at = 0.0

    @property
    def pid_hint(self) -> int:
        return os.getpid()

    def run_forever(self) -> None:
        LOGGER.info(
            "Starting worker runtime session=%s agent=%s launcher=%s host=%s",
            self.session_id,
            self.agent_name,
            self.launcher_id,
            socket.gethostname(),
        )
        self._register_with_retry()
        self._ensure_session_workspace()
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        self._set_status("idle")
        try:
            while not self._stop_event.is_set():
                job = self._poll_next_job()
                if job is None:
                    time.sleep(self.poll_interval_seconds)
                    continue
                self._handle_job(job)
        except KeyboardInterrupt:
            LOGGER.info("Worker interrupted, shutting down.")
        finally:
            self._stop_event.set()
            self._terminate_current_process()
            heartbeat_thread.join(timeout=2)

    def _register_with_retry(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.bridge_client.register_worker(
                    session_id=self.session_id,
                    agent_name=self.agent_name,
                    worker_id=self.worker_id,
                    launcher_id=self.launcher_id,
                    pid_hint=self.pid_hint,
                )
                LOGGER.info("Registered worker %s", self.worker_id)
                return
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Worker registration failed: %s", exc)
                time.sleep(5)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_interval_seconds):
            try:
                self._maybe_reconcile_workspace_from_bridge()
                self.bridge_client.heartbeat(
                    session_id=self.session_id,
                    agent_name=self.agent_name,
                    worker_id=self.worker_id,
                    status=self._get_status(),
                    pid_hint=self.pid_hint,
                    artifact_snapshot=self._build_artifact_snapshot(),
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Heartbeat failed: %s", exc)

    def _poll_next_job(self) -> dict[str, object] | None:
        try:
            return self.bridge_client.next_job(
                session_id=self.session_id,
                agent_name=self.agent_name,
                worker_id=self.worker_id,
            )
        except BridgeClientError as exc:
            LOGGER.error("Bridge polling failed: %s", exc)
            time.sleep(5)
            return None
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Bridge polling hit a transient error: %s", exc)
            time.sleep(5)
            return None

    def _handle_job(self, job: dict[str, object]) -> None:
        job_id = str(job["id"])
        job_type = str(job["job_type"])
        self._set_status("busy")

        try:
            if job_type == "restart":
                self._set_status("restarting")
                self._restart_runtime()
                self.bridge_client.complete_job(
                    job_id=job_id,
                    session_id=self.session_id,
                    agent_name=self.agent_name,
                    worker_id=self.worker_id,
                    output_text=f"Agent `{self.agent_name}` restarted successfully.",
                    pid_hint=self.pid_hint,
                )
            else:
                completion = self._run_cli_for_job(job)
                self.bridge_client.complete_job(
                    job_id=job_id,
                    session_id=self.session_id,
                    agent_name=self.agent_name,
                    worker_id=self.worker_id,
                    output_text=completion.control_text,
                    thread_output_text=completion.thread_text,
                    pid_hint=self.pid_hint,
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Job %s failed", job_id)
            try:
                self.bridge_client.fail_job(
                    job_id=job_id,
                    session_id=self.session_id,
                    agent_name=self.agent_name,
                    worker_id=self.worker_id,
                    error_text=str(exc),
                    pid_hint=self.pid_hint,
                )
            except Exception as report_exc:  # noqa: BLE001
                LOGGER.error("Failed to report job failure to bridge: %s", report_exc)
        finally:
            self._set_status("idle")

    def _run_cli_for_job(self, job: dict[str, object]) -> BridgeCompletionPayload:
        job_type = str(job.get("job_type") or "message")
        workspace = self._ensure_session_workspace(job)
        workspace.write_job_brief(
            agent_name=self.agent_name,
            job_type=job_type,
            user_text=str(job.get("input_text") or ""),
            session_summary=str(job["session_summary"]) if job.get("session_summary") is not None else None,
            recent_transcript=list(job.get("recent_transcript") or []),
        )
        context = AdapterContext(
            session_id=self.session_id,
            project_name=str(job.get("project_name") or self._session_name),
            agent_name=self.agent_name,
            job_type=job_type,
            system_prompt=self.system_prompt,
            user_text=str(job.get("input_text") or ""),
            open_tools=self.project.startup.open_tools,
            preset=str(job["preset"]) if job.get("preset") is not None else None,
            session_status=str(job.get("session_status") or "unknown"),
            session_summary=str(job["session_summary"]) if job.get("session_summary") is not None else None,
            project_workdir=str(self.project.default_workdir),
            session_workspace=str(workspace.root),
            session_workspace_relative=workspace.relative_root.as_posix(),
            available_agents=list(job.get("available_agents") or []),
            recent_transcript=list(job.get("recent_transcript") or []),
        )
        command = self.adapter.build_command()
        timeout_seconds = self._timeout_for_job(job_type)
        env = self._build_subprocess_env()
        env["SESSION_ID"] = self.session_id
        env["AGENT_NAME"] = self.agent_name
        env["WORKER_ID"] = self.worker_id

        LOGGER.info(
            "Launching %s adapter with command=%s timeout=%ss job_type=%s",
            self.agent.cli,
            command,
            timeout_seconds,
            job_type,
        )
        process = subprocess.Popen(
            command,
            cwd=self.project.default_workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **text_subprocess_kwargs(),
        )
        self._current_process = process
        try:
            stdout, stderr = process.communicate(
                input=self.adapter.prepare_input(context),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            failure_summary = workspace.record_cli_failure(
                agent_name=self.agent_name,
                job_type=job_type,
                user_text=str(job.get("input_text") or ""),
                summary=(
                    f"{self.agent.cli} timed out after {timeout_seconds}s while handling "
                    f"`{job_type}` work. The worker killed the hung subprocess."
                ),
                stdout_text=stdout,
                stderr_text=stderr,
                planner_recovery_expected=(job_type == "handoff" and self.agent_name.lower() != "planner"),
            )
            raise TimeoutError(failure_summary) from exc
        finally:
            self._current_process = None

        combined_output = self.adapter.combine_output(stdout, stderr, process.returncode or 0)
        if process.returncode not in (0, None):
            failure_summary = workspace.record_cli_failure(
                agent_name=self.agent_name,
                job_type=job_type,
                user_text=str(job.get("input_text") or ""),
                summary=(
                    f"{self.agent.cli} exited with code {process.returncode} while handling "
                    f"`{job_type}` work."
                ),
                stdout_text=stdout,
                stderr_text=stderr,
                planner_recovery_expected=(job_type == "handoff" and self.agent_name.lower() != "planner"),
            )
            raise RuntimeError(failure_summary)
        return workspace.record_cli_result(
            agent_name=self.agent_name,
            job_type=job_type,
            user_text=str(job.get("input_text") or ""),
            raw_output=combined_output,
        )

    def _restart_runtime(self) -> None:
        self._terminate_current_process()

    def _terminate_current_process(self) -> None:
        if self._current_process is None:
            return
        if self._current_process.poll() is None:
            self._current_process.terminate()
            try:
                self._current_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._current_process.kill()
        self._current_process = None

    def _set_status(self, status: str) -> None:
        with self._status_lock:
            self._status = status

    def _get_status(self) -> str:
        with self._status_lock:
            return self._status

    def _ensure_session_workspace(self, job: dict[str, object] | None = None) -> SessionWorkspace:
        if self._workspace is not None:
            return self._workspace

        session_details = self._load_session_metadata_with_retry()
        self._session_name = str(session_details.get("project_name") or self._session_name)
        self._session_preset = str(session_details.get("preset") or self._session_preset)
        available_agents = [
            str(agent.get("agent_name"))
            for agent in session_details.get("agents", [])
            if isinstance(agent, dict) and agent.get("agent_name")
        ]
        if not available_agents and job is not None:
            available_agents = [
                str(agent.get("agent_name"))
                for agent in job.get("available_agents", [])
                if isinstance(agent, dict) and agent.get("agent_name")
            ]
        if not available_agents:
            available_agents = [configured_agent.name for configured_agent in self.project.agents]

        self._workspace = SessionWorkspace.create(
            project_workdir=self.project.default_workdir,
            artifacts=self.project.artifacts,
            session_name=self._session_name,
            session_id=self.session_id,
            agent_names=available_agents,
        )
        self._workspace.ensure_structure()
        LOGGER.info("Using session workspace %s", self._workspace.root)
        return self._workspace

    def _load_session_metadata_with_retry(self) -> dict[str, object]:
        while not self._stop_event.is_set():
            try:
                return self.bridge_client.get_session(self.session_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to load session metadata: %s", exc)
                time.sleep(5)
        raise RuntimeError("Worker stopped before session metadata was available.")

    def _timeout_for_job(self, job_type: str) -> int:
        base_timeout = max(60, self.agent.timeout_seconds)
        if job_type == "restart":
            return min(base_timeout, RESTART_TIMEOUT_CAP_SECONDS)
        if job_type == "routing":
            return min(base_timeout, ROUTING_TIMEOUT_CAP_SECONDS)
        if job_type == "handoff":
            return min(base_timeout, HANDOFF_TIMEOUT_CAP_SECONDS)
        return base_timeout

    @staticmethod
    def _build_subprocess_env() -> dict[str, str]:
        # Force UTF-8 so non-ASCII prompts and reports survive Windows subprocess hops.
        return build_utf8_subprocess_env()

    def _build_artifact_snapshot(self) -> dict[str, object] | None:
        if self._workspace is None:
            return None
        try:
            return self._workspace.build_heartbeat_snapshot()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to build artifact heartbeat snapshot: %s", exc)
            return None

    def _maybe_reconcile_workspace_from_bridge(self) -> None:
        if self._workspace is None or not self.agent.default:
            return
        if self._get_status() == "busy":
            return
        now = time.monotonic()
        if now - self._last_workspace_reconcile_at < max(self.heartbeat_interval_seconds, 15):
            return
        self._last_workspace_reconcile_at = now
        try:
            summary = self.bridge_client.get_session(self.session_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to load session summary for artifact reconciliation: %s", exc)
            return
        try:
            changed = self._workspace.reconcile_from_bridge_summary(summary, agent_name=self.agent_name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Artifact reconciliation failed: %s", exc)
            return
        if changed:
            LOGGER.info("Reconciled local artifacts from bridge session summary for %s", self.session_id)
