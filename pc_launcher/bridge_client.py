from __future__ import annotations

import logging
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class BridgeClientError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, *, base_url: str, auth_token: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {auth_token}",
                "Content-Type": "application/json",
            },
        )

    def register_projects(self, *, launcher_id: str, hostname: str, projects: list[dict[str, object]]) -> dict[str, Any]:
        return self._post(
            "/api/sessions/projects/register",
            {"launcher_id": launcher_id, "hostname": hostname, "projects": projects},
        )

    def claim_launches(self, *, launcher_id: str, capacity: int) -> list[dict[str, Any]]:
        return self._post(
            "/api/sessions/launches/claim",
            {"launcher_id": launcher_id, "capacity": capacity},
        )

    def claim_project_finds(self, *, launcher_id: str, capacity: int = 1) -> list[dict[str, Any]]:
        return self._post(
            "/api/sessions/project-finds/claim",
            {"launcher_id": launcher_id, "capacity": capacity},
        )

    def complete_project_find(
        self,
        *,
        find_id: str,
        launcher_id: str,
        status: str,
        selected_path: str | None,
        selected_name: str | None,
        reason: str | None,
        confidence: float | None,
        candidates: list[dict[str, object]],
        error_text: str | None,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/sessions/project-finds/{find_id}/complete",
            {
                "launcher_id": launcher_id,
                "status": status,
                "selected_path": selected_path,
                "selected_name": selected_name,
                "reason": reason,
                "confidence": confidence,
                "candidates": candidates,
                "error_text": error_text,
            },
        )

    def claim_verification_runs(self, *, launcher_id: str, capacity: int = 1) -> list[dict[str, Any]]:
        return self._post(
            "/api/verification/claim",
            {"launcher_id": launcher_id, "capacity": capacity},
        )

    def complete_verification_run(
        self,
        *,
        run_id: str,
        launcher_id: str,
        status: str,
        summary_text: str | None,
        error_text: str | None,
        artifacts: list[dict[str, object]],
    ) -> dict[str, Any]:
        return self._post(
            f"/api/verification/runs/{run_id}/complete",
            {
                "launcher_id": launcher_id,
                "status": status,
                "summary_text": summary_text,
                "error_text": error_text,
                "artifacts": artifacts,
            },
        )

    def register_worker(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
        launcher_id: str,
        pid_hint: int | None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/workers/register",
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "worker_id": worker_id,
                "launcher_id": launcher_id,
                "pid_hint": pid_hint,
            },
        )

    def heartbeat(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
        status: str,
        pid_hint: int | None,
        artifact_snapshot: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/workers/heartbeat",
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "worker_id": worker_id,
                "status": status,
                "pid_hint": pid_hint,
                "artifact_snapshot": artifact_snapshot,
            },
        )

    def next_job(self, *, session_id: str, agent_name: str, worker_id: str) -> dict[str, Any] | None:
        payload = self._post(
            "/api/workers/next-job",
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "worker_id": worker_id,
            },
        )
        return payload.get("job")

    def complete_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        output_text: str,
        pid_hint: int | None,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workers/jobs/{job_id}/complete",
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "worker_id": worker_id,
                "output_text": output_text,
                "pid_hint": pid_hint,
            },
        )

    def fail_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        error_text: str,
        pid_hint: int | None,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workers/jobs/{job_id}/fail",
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "worker_id": worker_id,
                "error_text": error_text,
                "pid_hint": pid_hint,
            },
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/api/sessions/{session_id}")

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = self.session.post(
            f"{self.base_url}{path}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        return self._handle_response(path, response)

    def _get(self, path: str) -> Any:
        response = self.session.get(
            f"{self.base_url}{path}",
            timeout=self.timeout_seconds,
        )
        return self._handle_response(path, response)

    def _handle_response(self, path: str, response: requests.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        if not response.ok:
            raise BridgeClientError(f"{path} -> {response.status_code}: {payload}")
        return payload
