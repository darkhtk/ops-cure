from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote_plus

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
        activity_line: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "agent_name": agent_name,
            "worker_id": worker_id,
            "status": status,
            "pid_hint": pid_hint,
            "artifact_snapshot": artifact_snapshot,
        }
        if activity_line is not None:
            payload["activity_line"] = activity_line
        return self._post("/api/workers/heartbeat", payload)

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

    def get_thread_delta(
        self,
        *,
        session_id: str,
        agent_name: str,
        cursor: str | None = None,
        kinds: list[str] | None = None,
        task_id: str | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "agent_name": agent_name,
            "cursor": cursor,
            "kinds": kinds or [],
            "task_id": task_id,
            "limit": limit,
        }
        return self._post("/api/workers/thread-delta", payload)

    def complete_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        output_text: str,
        thread_output_text: str | None = None,
        lease_token: str | None = None,
        task_revision: int | None = None,
        session_epoch: int | None = None,
        pid_hint: int | None,
    ) -> dict[str, Any]:
        payload = {
            "session_id": session_id,
            "agent_name": agent_name,
            "worker_id": worker_id,
            "output_text": output_text,
            "pid_hint": pid_hint,
        }
        if thread_output_text is not None:
            payload["thread_output_text"] = thread_output_text
        if lease_token is not None:
            payload["lease_token"] = lease_token
        if task_revision is not None:
            payload["task_revision"] = task_revision
        if session_epoch is not None:
            payload["session_epoch"] = session_epoch
        return self._post(f"/api/workers/jobs/{job_id}/complete", payload)

    def fail_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        error_text: str,
        lease_token: str | None = None,
        task_revision: int | None = None,
        session_epoch: int | None = None,
        pid_hint: int | None,
    ) -> dict[str, Any]:
        payload = {
            "session_id": session_id,
            "agent_name": agent_name,
            "worker_id": worker_id,
            "error_text": error_text,
            "pid_hint": pid_hint,
        }
        if lease_token is not None:
            payload["lease_token"] = lease_token
        if task_revision is not None:
            payload["task_revision"] = task_revision
        if session_epoch is not None:
            payload["session_epoch"] = session_epoch
        return self._post(f"/api/workers/jobs/{job_id}/fail", payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/api/sessions/{session_id}")

    def get_space_by_thread(self, *, thread_id: str) -> dict[str, Any]:
        return self._get(f"/api/spaces/by-thread/{thread_id}")

    def get_actors_for_space(self, *, space_id: str) -> dict[str, Any]:
        return self._get(f"/api/actors/spaces/{space_id}")

    def get_events_for_space(self, *, space_id: str, limit: int = 20) -> dict[str, Any]:
        return self._get(f"/api/events/spaces/{space_id}?limit={limit}")

    def get_events_for_thread(
        self,
        *,
        thread_id: str,
        after_cursor: str | None = None,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        params = [f"limit={limit}"]
        if after_cursor:
            params.append(f"after_cursor={quote_plus(after_cursor)}")
        for kind in kinds or []:
            params.append(f"kinds={quote_plus(kind)}")
        return self._get(f"/api/events/threads/{thread_id}?{'&'.join(params)}")

    def stream_events_for_thread(
        self,
        *,
        thread_id: str,
        after_cursor: str | None = None,
        limit: int = 100,
        kinds: list[str] | None = None,
        subscriber_id: str | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        params = [f"limit={limit}"]
        if after_cursor:
            params.append(f"after_cursor={quote_plus(after_cursor)}")
        for kind in kinds or []:
            params.append(f"kinds={quote_plus(kind)}")
        if subscriber_id:
            params.append(f"subscriber_id={quote_plus(subscriber_id)}")
        response = self.session.get(
            f"{self.base_url}/api/events/threads/{thread_id}/stream?{'&'.join(params)}",
            timeout=(self.timeout_seconds, max(self.timeout_seconds, 60)),
            stream=True,
        )
        if not response.ok:
            self._handle_response(f"/api/events/threads/{thread_id}/stream", response)
        try:
            yield from self._iter_sse(response)
        finally:
            response.close()

    def stream_remote_codex_machine(
        self,
        *,
        machine_id: str,
        subscriber_id: str | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        params: list[str] = []
        if subscriber_id:
            params.append(f"subscriber_id={quote_plus(subscriber_id)}")
        query = f"?{'&'.join(params)}" if params else ""
        response = self.session.get(
            f"{self.base_url}/api/remote-codex/machines/{machine_id}/live{query}",
            timeout=(self.timeout_seconds, max(self.timeout_seconds, 60)),
            stream=True,
        )
        if not response.ok:
            self._handle_response(f"/api/remote-codex/machines/{machine_id}/live", response)
        try:
            yield from self._iter_sse(response)
        finally:
            response.close()

    def register_chat_participant(
        self,
        *,
        thread_id: str,
        actor_name: str,
        actor_kind: str = "ai",
    ) -> dict[str, Any]:
        return self._post(
            f"/api/chat/threads/{thread_id}/participants/register",
            {
                "actor_name": actor_name,
                "actor_kind": actor_kind,
            },
        )

    def heartbeat_chat_participant(
        self,
        *,
        thread_id: str,
        actor_name: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/chat/threads/{thread_id}/participants/heartbeat",
            {
                "actor_name": actor_name,
            },
        )

    def get_chat_delta(
        self,
        *,
        thread_id: str,
        actor_name: str,
        after_message_id: str | None = None,
        limit: int = 20,
        mark_read: bool = False,
    ) -> dict[str, Any]:
        params = [
            f"actor_name={quote_plus(actor_name)}",
            f"limit={limit}",
            f"mark_read={'true' if mark_read else 'false'}",
        ]
        if after_message_id:
            params.append(f"after_message_id={quote_plus(after_message_id)}")
        return self._get(f"/api/chat/threads/{thread_id}/delta?{'&'.join(params)}")

    def submit_chat_message(
        self,
        *,
        thread_id: str,
        actor_name: str,
        content: str,
        actor_kind: str = "ai",
    ) -> dict[str, Any]:
        return self._post(
            f"/api/chat/threads/{thread_id}/messages",
            {
                "actor_name": actor_name,
                "actor_kind": actor_kind,
                "content": content,
            },
        )

    def list_remote_tasks_for_machine(
        self,
        *,
        machine_id: str,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params = [f"limit={max(1, min(limit, 200))}"]
        for status in statuses or []:
            params.append(f"statuses={quote_plus(status)}")
        query = "&".join(params)
        payload = self._get(f"/api/remote/machines/{machine_id}/tasks?{query}")
        items = payload.get("tasks") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        return [self._normalize_remote_task(item) for item in items]

    def get_remote_task(self, *, task_id: str) -> dict[str, Any]:
        payload = self._get(f"/api/remote-codex/tasks/{task_id}")
        task = payload.get("task") if isinstance(payload, dict) else payload
        return self._normalize_remote_task(task)

    def claim_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_seconds: int = 90,
    ) -> dict[str, Any]:
        payload = self._post(
            f"/api/remote-codex/tasks/{task_id}/claim",
            {
                "actor_id": actor_id,
                "lease_seconds": lease_seconds,
            },
        )
        return self._normalize_remote_task(payload.get("task") if isinstance(payload, dict) else payload)

    def claim_next_remote_task_for_machine(
        self,
        *,
        machine_id: str,
        actor_id: str,
        lease_seconds: int = 90,
        exclude_origin_surfaces: list[str] | None = None,
    ) -> dict[str, Any] | None:
        payload = {
            "actor_id": actor_id,
            "lease_seconds": lease_seconds,
        }
        if exclude_origin_surfaces:
            payload["exclude_origin_surfaces"] = list(exclude_origin_surfaces)
        payload = self._post(
            f"/api/remote-codex/machines/{machine_id}/tasks/claim-next",
            payload,
        )
        task = payload.get("task") if isinstance(payload, dict) else payload
        if not task:
            return None
        return self._normalize_remote_task(task)

    def heartbeat_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        phase: str,
        summary: str,
        lease_seconds: int = 90,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
    ) -> dict[str, Any]:
        payload = {
            "actor_id": actor_id,
            "lease_token": lease_token,
            "phase": phase,
            "summary": summary,
            "lease_seconds": lease_seconds,
            "commands_run_count": commands_run_count,
            "files_read_count": files_read_count,
            "files_modified_count": files_modified_count,
            "tests_run_count": tests_run_count,
        }
        return self._post_with_fallback(
            f"/api/remote-codex/tasks/{task_id}/heartbeat",
            payload,
            fallback_path=f"/api/remote-codex/agent/tasks/{task_id}/heartbeat",
            fallback_payload={
                "actorId": actor_id,
                "phase": phase,
                "summary": summary,
                "commandsRunCount": commands_run_count,
                "filesReadCount": files_read_count,
                "filesModifiedCount": files_modified_count,
                "testsRunCount": tests_run_count,
            },
        )

    def add_remote_task_evidence(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_payload = {
            "actor_id": actor_id,
            "kind": kind,
            "summary": summary,
            "payload": payload or {},
        }
        return self._post_with_fallback(
            f"/api/remote-codex/tasks/{task_id}/evidence",
            request_payload,
            fallback_path=f"/api/remote-codex/agent/tasks/{task_id}/evidence",
            fallback_payload={
                "actorId": actor_id,
                "kind": kind,
                "summary": summary,
                "payload": payload or {},
            },
        )

    def add_remote_task_note(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        content: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/remote-codex/tasks/{task_id}/notes",
            {
                "actor_id": actor_id,
                "kind": kind,
                "content": content,
            },
        )

    def complete_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        summary: str,
    ) -> dict[str, Any]:
        payload = {
            "actor_id": actor_id,
            "lease_token": lease_token,
            "summary": summary,
        }
        return self._post_with_fallback(
            f"/api/remote-codex/tasks/{task_id}/complete",
            payload,
            fallback_path=f"/api/remote-codex/agent/tasks/{task_id}/complete",
            fallback_payload={
                "actorId": actor_id,
                "summary": summary,
            },
        )

    def fail_remote_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        lease_token: str,
        error_text: str,
    ) -> dict[str, Any]:
        payload = {
            "actor_id": actor_id,
            "lease_token": lease_token,
            "error_text": error_text,
        }
        return self._post_with_fallback(
            f"/api/remote-codex/tasks/{task_id}/fail",
            payload,
            fallback_path=f"/api/remote-codex/agent/tasks/{task_id}/fail",
            fallback_payload={
                "actorId": actor_id,
                "error": {"message": error_text},
            },
        )

    def sync_remote_codex_agent(
        self,
        *,
        machine: dict[str, Any],
        threads: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._post(
            "/api/remote-codex/agent/sync",
            {
                "machine": machine,
                "threads": threads,
                "snapshots": snapshots,
            },
        )

    def get_remote_codex_thread_commands(
        self,
        *,
        machine_id: str,
        thread_id: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        payload = self._get(
            f"/api/remote-codex/machines/{machine_id}/threads/{thread_id}/commands",
            {"limit": max(1, min(int(limit), 32))},
        )
        return list(payload.get("commands") or [])

    def claim_next_remote_codex_command(
        self,
        *,
        machine_id: str,
        worker_id: str,
    ) -> dict[str, Any] | None:
        payload = self._post(
            "/api/remote-codex/agent/commands/claim",
            {
                "machineId": machine_id,
                "workerId": worker_id,
            },
        )
        return payload.get("command")

    def report_remote_codex_command_result(
        self,
        *,
        command_id: str,
        worker_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "workerId": worker_id,
            "status": status,
        }
        if result is not None:
            body["result"] = result
        if error is not None:
            body["error"] = error
        return self._post(
            f"/api/remote-codex/agent/commands/{command_id}/result",
            body,
        )

    def heartbeat_remote_codex_agent_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        phase: str,
        summary: str,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/remote-codex/agent/tasks/{task_id}/heartbeat",
            {
                "actorId": actor_id,
                "phase": phase,
                "summary": summary,
                "commandsRunCount": commands_run_count,
                "filesReadCount": files_read_count,
                "filesModifiedCount": files_modified_count,
                "testsRunCount": tests_run_count,
            },
        )

    def add_remote_codex_agent_task_evidence(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/remote-codex/agent/tasks/{task_id}/evidence",
            {
                "actorId": actor_id,
                "kind": kind,
                "summary": summary,
                "payload": payload or {},
            },
        )

    def complete_remote_codex_agent_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        summary: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/remote-codex/agent/tasks/{task_id}/complete",
            {
                "actorId": actor_id,
                "summary": summary,
            },
        )

    def fail_remote_codex_agent_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        error_text: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/remote-codex/agent/tasks/{task_id}/fail",
            {
                "actorId": actor_id,
                "error": {"message": error_text},
            },
        )

    def _normalize_remote_task(self, task: dict[str, Any] | Any) -> dict[str, Any]:
        if not isinstance(task, dict):
            return {}
        if "id" in task and "machine_id" in task:
            return task
        current_claim = task.get("currentClaim") if isinstance(task.get("currentClaim"), dict) else {}
        latest_approval = task.get("latestApproval") if isinstance(task.get("latestApproval"), dict) else None
        latest_heartbeat = task.get("latestHeartbeat") if isinstance(task.get("latestHeartbeat"), dict) else None
        return {
            "id": task.get("taskId"),
            "machine_id": task.get("machineId"),
            "thread_id": task.get("threadId"),
            "origin_surface": task.get("sourceSurface"),
            "objective": task.get("objective"),
            "priority": task.get("priority"),
            "owner_actor_id": task.get("ownerActorId"),
            "created_by": task.get("createdBy"),
            "status": task.get("status"),
            "success_criteria": {},
            "current_assignment": (
                {
                    "actor_id": current_claim.get("actorId"),
                    "lease_token": current_claim.get("leaseToken"),
                    "claimed_at": current_claim.get("claimedAt"),
                    "lease_expires_at": current_claim.get("leaseExpiresAt"),
                }
                if current_claim
                else None
            ),
            "latest_approval": latest_approval,
            "latest_heartbeat": latest_heartbeat,
            "recent_evidence": task.get("recentEvidence") or [],
        }

    # ---------- Generic kernel scratch (KernelScratchService HTTP wrapper) ----------

    def get_kernel_scratch(
        self,
        *,
        key: str,
        actor_id: str = "",
        space_id: str = "",
        default: Any = None,
    ) -> Any:
        """Read a kernel scratch entry. Returns ``default`` when the entry
        is missing or expired."""
        params = {"key": key, "actor_id": actor_id, "space_id": space_id}
        try:
            payload = self._get_with_params("/api/kernel/scratch", params)
        except BridgeClientError:
            return default
        if not isinstance(payload, dict) or not payload.get("found"):
            return default
        return payload.get("value", default)

    def set_kernel_scratch(
        self,
        *,
        key: str,
        value: Any,
        actor_id: str = "",
        space_id: str = "",
        ttl_seconds: int | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "actor_id": actor_id,
            "space_id": space_id,
            "key": key,
            "value": value,
        }
        if ttl_seconds is not None:
            body["ttl_seconds"] = int(ttl_seconds)
        self._put("/api/kernel/scratch", body)

    def delete_kernel_scratch(
        self,
        *,
        key: str,
        actor_id: str = "",
        space_id: str = "",
    ) -> bool:
        body = {"actor_id": actor_id, "space_id": space_id, "key": key}
        try:
            payload = self._delete_with_body("/api/kernel/scratch", body)
        except BridgeClientError:
            return False
        return bool(isinstance(payload, dict) and payload.get("removed"))

    # -----------------------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = self.session.post(
            f"{self.base_url}{path}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        return self._handle_response(path, response)

    def _put(self, path: str, payload: dict[str, Any]) -> Any:
        response = self.session.put(
            f"{self.base_url}{path}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        return self._handle_response(path, response)

    def _delete_with_body(self, path: str, payload: dict[str, Any]) -> Any:
        response = self.session.delete(
            f"{self.base_url}{path}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        return self._handle_response(path, response)

    def _get_with_params(self, path: str, params: dict[str, Any]) -> Any:
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout_seconds,
        )
        return self._handle_response(path, response)

    def _post_with_fallback(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        fallback_path: str,
        fallback_payload: dict[str, Any],
    ) -> Any:
        try:
            return self._post(path, payload)
        except BridgeClientError as error:
            if not self._is_not_found_surface_error(error):
                raise
            LOGGER.warning(
                "Bridge surface %s unavailable, falling back to %s",
                path,
                fallback_path,
            )
            return self._post(fallback_path, fallback_payload)

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

    @staticmethod
    def _is_not_found_surface_error(error: BridgeClientError) -> bool:
        message = str(error)
        return "-> 404:" in message or "-> 410:" in message

    def _iter_sse(self, response: requests.Response) -> Iterator[tuple[str, dict[str, Any]]]:
        event_name = "message"
        data_lines: list[str] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            line = raw_line if raw_line is not None else ""
            if not line:
                if data_lines:
                    payload = json.loads("\n".join(data_lines))
                    yield event_name, payload
                event_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value or "message"
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            payload = json.loads("\n".join(data_lines))
            yield event_name, payload
