"""Behavior service for browser-first remote Codex state and control."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ...schemas import (
    RemoteTaskApprovalRequest,
    RemoteTaskApprovalResolveRequest,
    RemoteTaskClaimNextRequest,
    RemoteTaskClaimRequest,
    RemoteTaskCompleteRequest,
    RemoteTaskCreateRequest,
    RemoteTaskEvidenceRequest,
    RemoteTaskFailRequest,
    RemoteTaskHeartbeatRequest,
    RemoteTaskInterruptRequest,
    RemoteTaskNoteRequest,
    RemoteTaskSummaryResponse,
)
from ...services.remote_task_service import RemoteTaskService
from .state_service import (
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_QUEUED,
    COMMAND_RUNNING,
    THREAD_DELETE,
    TURN_INTERRUPT,
    TURN_START,
    RemoteCodexStateService,
    compact_text,
)

STALE_BROWSER_TASK_GRACE_SECONDS = 1800


def normalize_thread_status(status: Any) -> str | None:
    raw_status = (
        status
        if isinstance(status, str)
        else status.get("type")
        if isinstance(status, dict)
        else None
    )
    if raw_status == "active":
        return "inProgress"
    if raw_status == "notLoaded":
        return "idle"
    return raw_status


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_browser_turn_attachments(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        try:
            size = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        name = compact_text(item.get("name"), "attachment")
        mime_type = compact_text(item.get("mimeType") or item.get("mime_type"), "application/octet-stream")
        kind = compact_text(item.get("kind")).lower()
        if not kind:
            kind = "image" if mime_type.startswith("image/") else "file"
        if kind == "image":
            data_url = compact_text(item.get("dataUrl") or item.get("data_url"))
            if not data_url:
                continue
            normalized.append(
                {
                    "name": name,
                    "mimeType": mime_type,
                    "kind": "image",
                    "size": size,
                    "dataUrl": data_url,
                }
            )
            continue
        bytes_base64 = compact_text(item.get("bytesBase64") or item.get("bytes_base64"))
        if not bytes_base64:
            continue
        normalized.append(
            {
                    "name": name,
                    "mimeType": mime_type,
                    "kind": "file",
                    "size": size,
                    "bytesBase64": bytes_base64,
                }
            )
    return normalized


def build_browser_turn_prompt(prompt: str, attachments: list[dict[str, Any]]) -> str:
    normalized_prompt = compact_text(prompt)
    if normalized_prompt:
        return normalized_prompt
    if not attachments:
        return ""
    attachment_names = [compact_text(item.get("name")) for item in attachments if compact_text(item.get("name"))]
    if attachment_names:
        return f"Use the attached context: {', '.join(attachment_names[:3])}"
    return "Use the attached files and images as context for this turn."


class RemoteCodexBehaviorService:
    """Behavior service that exposes browser-compatible remote_codex surfaces."""

    behavior_id = "remote_codex"
    canonical_owner = "Opscure remote_codex behavior"

    def __init__(
        self,
        *,
        remote_task_service: RemoteTaskService | None = None,
        state_service: RemoteCodexStateService | None = None,
        kernel_subscription_broker: Any | None = None,
    ) -> None:
        self.remote_task_service = remote_task_service or RemoteTaskService()
        self.state_service = state_service or RemoteCodexStateService(
            kernel_subscription_broker=kernel_subscription_broker,
        )

    def _task_to_browser(self, task: RemoteTaskSummaryResponse) -> dict[str, Any]:
        latest_approval = None
        if task.latest_approval is not None:
            approval_status = task.latest_approval.status
            if approval_status == "resolved":
                if task.latest_approval.resolution == "approved":
                    approval_status = "approved"
                elif task.latest_approval.resolution in {"denied", "rejected"}:
                    approval_status = "rejected"
            latest_approval = {
                "id": task.latest_approval.id,
                "actorId": task.latest_approval.actor_id,
                "reason": task.latest_approval.reason,
                "status": approval_status,
                "note": task.latest_approval.note,
                "requestedAt": task.latest_approval.requested_at.isoformat(),
                "resolvedAt": task.latest_approval.resolved_at.isoformat()
                if task.latest_approval.resolved_at
                else None,
                "resolvedBy": task.latest_approval.resolved_by,
                "resolution": task.latest_approval.resolution,
            }

        current_claim = None
        if task.current_assignment is not None:
            current_claim = {
                "actorId": task.current_assignment.actor_id,
                "workerId": None,
                "leaseToken": task.current_assignment.lease_token,
                "claimedAt": task.current_assignment.claimed_at.isoformat(),
                "leaseExpiresAt": task.current_assignment.lease_expires_at.isoformat(),
            }

        latest_heartbeat = None
        if task.latest_heartbeat is not None:
            latest_heartbeat = {
                "actorId": task.latest_heartbeat.actor_id,
                "phase": task.latest_heartbeat.phase,
                "summary": task.latest_heartbeat.summary,
                "commandsRunCount": task.latest_heartbeat.commands_run_count,
                "filesReadCount": task.latest_heartbeat.files_read_count,
                "filesModifiedCount": task.latest_heartbeat.files_modified_count,
                "testsRunCount": task.latest_heartbeat.tests_run_count,
                "createdAt": task.latest_heartbeat.created_at.isoformat(),
            }

        recent_evidence = [
            {
                "id": item.id,
                "actorId": item.actor_id,
                "kind": item.kind,
                "summary": item.summary,
                "payload": item.payload,
                "createdAt": item.created_at.isoformat(),
            }
            for item in task.recent_evidence
        ]

        return {
            "taskId": task.id,
            "machineId": task.machine_id,
            "threadId": task.thread_id,
            "sourceSurface": task.origin_surface,
            "objective": task.objective,
            "status": task.status,
            "priority": task.priority,
            "ownerActorId": task.owner_actor_id,
            "createdBy": task.created_by,
            "createdAt": task.created_at.isoformat(),
            "updatedAt": task.updated_at.isoformat(),
            "currentClaim": current_claim,
            "latestHeartbeat": latest_heartbeat,
            "recentEvidence": recent_evidence,
            "latestApproval": latest_approval,
        }

    def _requested_by(
        self,
        *,
        auth_method: str | None,
        email: str | None,
        name: str | None,
        subject: str | None = None,
        asserted_client_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "authMethod": compact_text(auth_method, "unknown"),
            "email": compact_text(email) or None,
            "name": compact_text(name) or None,
            "subject": compact_text(subject) or None,
            "assertedClientId": compact_text(asserted_client_id) or None,
        }

    def _created_by(self, requested_by: dict[str, Any]) -> str:
        return (
            compact_text(requested_by.get("email"))
            or compact_text(requested_by.get("name"))
            or compact_text(requested_by.get("subject"))
            or "bridge-service"
        )

    def get_health(self) -> dict[str, Any]:
        machine_summary = self.state_service.get_machine_summary(active_only=True)
        control = self.get_control_status()
        return {
            "ok": True,
            "mode": control["mode"],
            "remoteCodex": {
                "canonicalOwner": self.canonical_owner,
                "bridgeMode": "opscure-canonical",
                "localBridgeTransitional": False,
            },
            "machineSummary": machine_summary,
            "machines": self.state_service.list_machines(active_only=True),
        }

    def get_control_status(self) -> dict[str, Any]:
        machines = self.state_service.list_machines(active_only=True)
        machine_summary = self.state_service.get_machine_summary(active_only=True)
        online_machines = machine_summary["onlineMachines"]
        live_control_machines = len(
            [
                machine
                for machine in machines
                if machine["status"] == "online" and machine.get("capabilities", {}).get("liveControl")
            ]
        )
        if online_machines > 0 and live_control_machines > 0:
            return {
                "supported": True,
                "mode": "live-control",
                "activeTransport": "opscure-remote-codex",
                "reason": "Machine-scoped reading and live turn control are available through Opscure remote_codex.",
                "detail": "The browser is reading machine, thread, transcript, and task state directly from the Opscure remote_codex behavior.",
                "capabilities": {
                    "multiMachineRead": True,
                    "liveControl": True,
                },
                "machineSummary": machine_summary,
            }
        if online_machines > 0:
            return {
                "supported": False,
                "mode": "read-only",
                "activeTransport": "opscure-remote-codex",
                "reason": "Machines are online, but none currently report live turn control.",
                "detail": "The browser can browse cached remote Codex state, but turn submission remains disabled until a machine reports liveControl support.",
                "capabilities": {
                    "multiMachineRead": True,
                    "liveControl": False,
                },
                "machineSummary": machine_summary,
            }
        return {
            "supported": False,
            "mode": "read-only",
            "activeTransport": "opscure-remote-codex",
            "reason": "No remote Codex machine is currently online.",
            "detail": "Start a remote executor or machine sync source so the browser has at least one machine to read.",
            "capabilities": {
                "multiMachineRead": False,
                "liveControl": False,
            },
            "machineSummary": machine_summary,
        }

    def list_machines(self) -> dict[str, Any]:
        return {"machines": self.state_service.list_machines(active_only=True)}

    def get_machine(self, machine_id: str) -> dict[str, Any] | None:
        return self.state_service.get_machine(machine_id)

    def list_machine_threads(self, machine_id: str, *, query: str = "", limit: int = 60) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        return {
            "machine": machine,
            "threads": self.state_service.get_threads(machine_id, query=query, limit=limit) or [],
        }

    def get_thread(self, machine_id: str, thread_id: str) -> dict[str, Any]:
        thread = self.state_service.get_thread(machine_id, thread_id)
        if thread is None:
            raise ValueError("thread_not_found")
        return thread

    def get_thread_messages(
        self,
        machine_id: str,
        thread_id: str,
        *,
        limit: int = 250,
        after_line_number: int = 0,
    ) -> dict[str, Any]:
        snapshot = self.state_service.get_thread_snapshot(
            machine_id,
            thread_id,
            limit=limit,
            after_line_number=after_line_number,
        )
        if snapshot is None:
            raise ValueError("thread_not_found")
        return snapshot

    def get_thread_commands(self, machine_id: str, thread_id: str, *, limit: int = 8) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        thread = self.state_service.get_thread(machine_id, thread_id)
        if thread is None:
            raise ValueError("thread_not_found")
        return {
            "machine": machine,
            "thread": thread,
            "commands": self.state_service.list_thread_commands(machine_id, thread_id, limit=limit),
        }

    def list_thread_tasks(
        self,
        machine_id: str,
        thread_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        thread = self.state_service.get_thread(machine_id, thread_id)
        if thread is None:
            raise ValueError("thread_not_found")
        tasks = self.remote_task_service.list_tasks(
            machine_id=machine_id,
            thread_id=thread_id,
            statuses=statuses,
            limit=limit,
        )
        tasks = self._cleanup_stale_thread_tasks(
            machine_id=machine_id,
            thread_id=thread_id,
            thread=thread,
            tasks=tasks,
            statuses=statuses,
            limit=limit,
        )
        return {
            "machine": machine,
            "thread": thread,
            "tasks": [self._task_to_browser(task) for task in tasks],
        }

    def create_task(self, payload: RemoteTaskCreateRequest) -> dict[str, Any]:
        created = self.remote_task_service.create_task(payload)
        task = self._task_to_browser(created)
        self.state_service._publish(task["machineId"], task["threadId"], {"kind": "task", "task": task})
        return {"ok": True, "task": task}

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.remote_task_service.get_task(task_id)
        thread = self.state_service.get_thread(task.machine_id, task.thread_id)
        if thread is not None:
            command = self._find_command_for_task(task.machine_id, task.thread_id, task.id)
            if command is not None:
                refreshed = self._maybe_cleanup_stale_task(
                    task=task,
                    command=command,
                    thread=thread,
                    latest_turn_id=self.state_service.get_latest_turn_id(task.machine_id, task.thread_id),
                )
                if refreshed is not None:
                    task = refreshed
        return {"task": self._task_to_browser(task)}

    def _find_command_for_task(self, machine_id: str, thread_id: str, task_id: str) -> dict[str, Any] | None:
        commands = self.state_service.list_thread_commands(machine_id, thread_id, limit=200, include_stale=True)
        for command in commands:
            if compact_text(command.get("taskId")) == task_id:
                return command
        return None

    def _cleanup_stale_thread_tasks(
        self,
        *,
        machine_id: str,
        thread_id: str,
        thread: dict[str, Any],
        tasks: list[RemoteTaskSummaryResponse],
        statuses: list[str] | None,
        limit: int,
    ) -> list[RemoteTaskSummaryResponse]:
        if not tasks:
            return tasks
        commands = self.state_service.list_thread_commands(machine_id, thread_id, limit=200, include_stale=True)
        commands_by_task_id = {
            compact_text(command.get("taskId")): command
            for command in commands
            if compact_text(command.get("taskId"))
        }
        latest_turn_id = self.state_service.get_latest_turn_id(machine_id, thread_id)
        changed = False
        refreshed: list[RemoteTaskSummaryResponse] = []
        for task in tasks:
            next_task = self._maybe_cleanup_stale_task(
                task=task,
                command=commands_by_task_id.get(task.id),
                thread=thread,
                latest_turn_id=latest_turn_id,
            )
            if next_task is not None:
                refreshed.append(next_task)
                if next_task.status != task.status or next_task.updated_at != task.updated_at:
                    changed = True
            else:
                refreshed.append(task)
        if not changed:
            return tasks
        return self.remote_task_service.list_tasks(
            machine_id=machine_id,
            thread_id=thread_id,
            statuses=statuses,
            limit=limit,
        )

    def _maybe_cleanup_stale_task(
        self,
        *,
        task: RemoteTaskSummaryResponse,
        command: dict[str, Any] | None,
        thread: dict[str, Any],
        latest_turn_id: str | None,
    ) -> RemoteTaskSummaryResponse | None:
        if task.origin_surface != "browser":
            return None
        if task.status not in {"claimed", "executing", "verifying"}:
            return None

        assignment = task.current_assignment
        now = datetime.now(timezone.utc)
        lease_expired = bool(
            assignment is not None
            and ensure_utc(assignment.lease_expires_at) is not None
            and ensure_utc(assignment.lease_expires_at) <= now
        )
        updated_at = ensure_utc(task.updated_at) or now
        age_seconds = max(0.0, (now - updated_at).total_seconds())
        thread_status = normalize_thread_status(thread.get("status"))

        if command is None:
            if lease_expired and age_seconds >= STALE_BROWSER_TASK_GRACE_SECONDS:
                cleaned = self.remote_task_service.settle_stale_task(
                    task.id,
                    final_status="failed",
                    summary="Stale browser task cleanup: the task lost its command tracking before completion.",
                    payload={"reason": "missing_command"},
                )
                self.state_service._publish(cleaned.machine_id, cleaned.thread_id, {"kind": "task", "task": self._task_to_browser(cleaned)})
                return cleaned
            return None

        command_status = compact_text(command.get("status")).lower()
        command_type = compact_text(command.get("type")).lower()
        command_turn_id = compact_text(command.get("turnId")) or None
        command_result = command.get("result") if isinstance(command.get("result"), dict) else {}
        command_turn_status = compact_text(command_result.get("turnStatus")).lower()

        if command_status == COMMAND_FAILED:
            cleaned = self.remote_task_service.settle_stale_task(
                task.id,
                final_status="failed",
                summary="Remote command failed before the browser task could finish.",
                payload={"reason": "command_failed", "commandId": command.get("commandId")},
            )
            self.state_service._publish(cleaned.machine_id, cleaned.thread_id, {"kind": "task", "task": self._task_to_browser(cleaned)})
            return cleaned

        if command_type == TURN_START and command_status == COMMAND_COMPLETED:
            cleaned = self.remote_task_service.settle_stale_task(
                task.id,
                final_status="completed",
                summary="Turn request accepted by the local Codex runtime.",
                payload={
                    "reason": "turn_command_completed",
                    "commandId": command.get("commandId"),
                    "turnId": command_turn_id,
                    "latestTurnId": latest_turn_id,
                    "turnStatus": command_turn_status,
                    "threadStatus": thread_status,
                },
            )
            self.state_service._publish(cleaned.machine_id, cleaned.thread_id, {"kind": "task", "task": self._task_to_browser(cleaned)})
            return cleaned

        if lease_expired and age_seconds >= STALE_BROWSER_TASK_GRACE_SECONDS and command_status in {COMMAND_QUEUED, COMMAND_RUNNING}:
            cleaned = self.remote_task_service.settle_stale_task(
                task.id,
                final_status="failed",
                summary="Stale browser task cleanup: the queued command stopped progressing after its lease expired.",
                payload={
                    "reason": "expired_command",
                    "commandId": command.get("commandId"),
                    "commandStatus": command_status,
                },
            )
            self.state_service._publish(cleaned.machine_id, cleaned.thread_id, {"kind": "task", "task": self._task_to_browser(cleaned)})
            return cleaned
        return None

    def claim_task(self, task_id: str, payload: RemoteTaskClaimRequest) -> dict[str, Any]:
        task = self.remote_task_service.claim_task(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def claim_next_machine_task(self, machine_id: str, payload: RemoteTaskClaimNextRequest) -> dict[str, Any] | None:
        task = self.remote_task_service.claim_next_task(machine_id=machine_id, payload=payload)
        if task is None:
            return None
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def heartbeat_task(self, task_id: str, payload: RemoteTaskHeartbeatRequest) -> dict[str, Any]:
        task = self.remote_task_service.heartbeat_task(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def add_evidence(self, task_id: str, payload: RemoteTaskEvidenceRequest) -> dict[str, Any]:
        task = self.remote_task_service.add_evidence(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def resolve_approval(self, task_id: str, payload: RemoteTaskApprovalResolveRequest) -> dict[str, Any]:
        task = self.remote_task_service.resolve_approval(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def request_approval(self, task_id: str, payload: RemoteTaskApprovalRequest) -> dict[str, Any]:
        task = self.remote_task_service.request_approval(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def add_note(self, task_id: str, payload: RemoteTaskNoteRequest) -> dict[str, Any]:
        note = self.remote_task_service.add_note(task_id, payload)
        return {
            "note": {
                "id": note.id,
                "actorId": note.actor_id,
                "kind": note.kind,
                "content": note.content,
                "createdAt": note.created_at.isoformat(),
            }
        }

    def list_notes(self, task_id: str) -> dict[str, Any]:
        notes = self.remote_task_service.list_notes(task_id)
        return {
            "notes": [
                {
                    "id": note.id,
                    "actorId": note.actor_id,
                    "kind": note.kind,
                    "content": note.content,
                    "createdAt": note.created_at.isoformat(),
                }
                for note in notes
            ]
        }

    def interrupt_task(self, task_id: str, payload: RemoteTaskInterruptRequest) -> dict[str, Any]:
        task = self.remote_task_service.interrupt_task(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def complete_task(self, task_id: str, payload: RemoteTaskCompleteRequest) -> dict[str, Any]:
        task = self.remote_task_service.complete_task(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def fail_task(self, task_id: str, payload: RemoteTaskFailRequest) -> dict[str, Any]:
        task = self.remote_task_service.fail_task(task_id, payload)
        browser_task = self._task_to_browser(task)
        self.state_service._publish(browser_task["machineId"], browser_task["threadId"], {"kind": "task", "task": browser_task})
        return {"ok": True, "task": browser_task}

    def apply_agent_sync(self, *, machine: dict[str, Any], threads: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
        return {"ok": True, "machine": self.state_service.apply_agent_sync(machine=machine, threads=threads, snapshots=snapshots)}

    def claim_next_command(self, *, machine_id: str, worker_id: str) -> dict[str, Any]:
        command = self.state_service.claim_next_command(machine_id, worker_id=worker_id)
        if command is None:
            return {"command": None}
        if command.get("taskId"):
            task = self.remote_task_service.claim_task(
                command["taskId"],
                RemoteTaskClaimRequest(actor_id=machine_id, lease_seconds=120),
            )
            browser_task = self._task_to_browser(task)
            self.state_service._publish(command["machineId"], command["threadId"], {"kind": "task", "task": browser_task})
        return {"command": command}

    def record_command_result(
        self,
        command_id: str,
        *,
        worker_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = compact_text(status).lower()
        if normalized == COMMAND_COMPLETED:
            command = self.state_service.complete_command(command_id, worker_id=worker_id, result=result)
            if command.get("taskId") and command["type"] in {TURN_START, TURN_INTERRUPT}:
                task = self.remote_task_service.get_task(command["taskId"])
                if task.current_assignment is not None:
                    summary = (
                        "Turn request accepted by the local Codex runtime."
                        if command["type"] == TURN_START
                        else "Interrupt request completed."
                    )
                    self.complete_task(
                        command["taskId"],
                        RemoteTaskCompleteRequest(
                            actor_id=command["machineId"],
                            lease_token=task.current_assignment.lease_token,
                            summary=summary,
                        ),
                    )
        else:
            command = self.state_service.fail_command(command_id, worker_id=worker_id, error=error)
            if command.get("taskId"):
                task = self.remote_task_service.get_task(command["taskId"])
                if task.current_assignment is not None:
                    self.fail_task(
                        command["taskId"],
                        RemoteTaskFailRequest(
                            actor_id=command["machineId"],
                            lease_token=task.current_assignment.lease_token,
                            error_text=compact_text((error or {}).get("message"), "Unknown error"),
                        ),
                    )
        return {"ok": True, "command": command}

    def agent_heartbeat_task(
        self,
        task_id: str,
        *,
        actor_id: str,
        phase: str,
        summary: str | None,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
    ) -> dict[str, Any]:
        task = self.remote_task_service.get_task(task_id)
        if task.current_assignment is None:
            raise ValueError("task_not_claimed")
        return self.heartbeat_task(
            task_id,
            RemoteTaskHeartbeatRequest(
                actor_id=actor_id,
                lease_token=task.current_assignment.lease_token,
                phase=phase,
                summary=summary,
                commands_run_count=commands_run_count,
                files_read_count=files_read_count,
                files_modified_count=files_modified_count,
                tests_run_count=tests_run_count,
            ),
        )

    def agent_add_evidence(
        self,
        task_id: str,
        *,
        actor_id: str,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.add_evidence(
            task_id,
            RemoteTaskEvidenceRequest(
                actor_id=actor_id,
                kind=kind,
                summary=summary,
                payload=payload or {},
            ),
        )

    def agent_complete_task(self, task_id: str, *, actor_id: str, summary: str | None) -> dict[str, Any]:
        task = self.remote_task_service.get_task(task_id)
        if task.current_assignment is None:
            raise ValueError("task_not_claimed")
        return self.complete_task(
            task_id,
            RemoteTaskCompleteRequest(
                actor_id=actor_id,
                lease_token=task.current_assignment.lease_token,
                summary=summary,
            ),
        )

    def agent_fail_task(self, task_id: str, *, actor_id: str, error: dict[str, Any] | None) -> dict[str, Any]:
        task = self.remote_task_service.get_task(task_id)
        if task.current_assignment is None:
            raise ValueError("task_not_claimed")
        return self.fail_task(
            task_id,
            RemoteTaskFailRequest(
                actor_id=actor_id,
                lease_token=task.current_assignment.lease_token,
                error_text=compact_text((error or {}).get("message"), "Unknown error"),
            ),
        )

    def enqueue_turn(
        self,
        *,
        machine_id: str,
        thread_id: str,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        if machine["status"] != "online":
            raise ValueError("machine_offline")
        if not machine.get("capabilities", {}).get("liveControl"):
            raise ValueError("machine_live_control_unavailable")
        thread = self.state_service.get_thread(machine_id, thread_id)
        if thread is None:
            raise ValueError("thread_not_found")
        normalized_attachments = normalize_browser_turn_attachments(attachments)
        prompt_for_command = build_browser_turn_prompt(prompt, normalized_attachments)
        if not prompt_for_command:
            raise ValueError("missing_prompt")
        requested_by_payload = dict(requested_by)
        if normalized_attachments:
            requested_by_payload["attachments"] = normalized_attachments

        task = self.remote_task_service.create_task(
            RemoteTaskCreateRequest(
                machine_id=machine_id,
                thread_id=thread_id,
                objective=prompt_for_command,
                success_criteria={"browser": ["task row", "command queue", "transcript update"]},
                created_by=self._created_by(requested_by),
                origin_surface="browser",
            )
        )
        command = self.state_service.enqueue_command(
            command_type=TURN_START,
            machine_id=machine_id,
            thread_id=thread_id,
            requested_by=requested_by_payload,
            prompt=prompt_for_command,
            task_id=task.id,
        )
        browser_task = self._task_to_browser(task)
        self.state_service._publish(machine_id, thread_id, {"kind": "task", "task": browser_task})
        return {
            "ok": True,
            "task": browser_task,
            "command": command,
        }

    def enqueue_interrupt(
        self,
        *,
        machine_id: str,
        thread_id: str,
        requested_by: dict[str, Any],
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        if machine["status"] != "online":
            raise ValueError("machine_offline")
        if not machine.get("capabilities", {}).get("liveControl"):
            raise ValueError("machine_live_control_unavailable")
        thread = self.state_service.get_thread(machine_id, thread_id)
        if thread is None:
            raise ValueError("thread_not_found")
        active_command = self.state_service.get_active_thread_command(machine_id, thread_id, command_type=TURN_INTERRUPT)
        if active_command is not None:
            raise RuntimeError("interrupt_command_in_progress")
        next_turn_id = compact_text(turn_id) or self.state_service.get_latest_turn_id(machine_id, thread_id)
        if not next_turn_id:
            raise ValueError("missing_turn_id")

        task = self.remote_task_service.create_task(
            RemoteTaskCreateRequest(
                machine_id=machine_id,
                thread_id=thread_id,
                objective=f"Interrupt turn {next_turn_id}",
                success_criteria={"browser": ["interrupt task", "command result"]},
                created_by=self._created_by(requested_by),
                origin_surface="browser",
            )
        )
        command = self.state_service.enqueue_command(
            command_type=TURN_INTERRUPT,
            machine_id=machine_id,
            thread_id=thread_id,
            requested_by=requested_by,
            turn_id=next_turn_id,
            task_id=task.id,
        )
        browser_task = self._task_to_browser(task)
        self.state_service._publish(machine_id, thread_id, {"kind": "task", "task": browser_task})
        return {
            "ok": True,
            "task": browser_task,
            "command": command,
        }

    def enqueue_thread_delete(
        self,
        *,
        machine_id: str,
        thread_id: str,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        if machine["status"] != "online":
            raise ValueError("machine_offline")
        thread = self.state_service.get_thread(machine_id, thread_id)
        if thread is None:
            raise ValueError("thread_not_found")
        active_command = self.state_service.get_active_thread_command(machine_id, thread_id)
        if active_command is not None:
            raise RuntimeError("turn_command_in_progress")

        command = self.state_service.enqueue_command(
            command_type=THREAD_DELETE,
            machine_id=machine_id,
            thread_id=thread_id,
            requested_by=requested_by,
        )
        return {
            "ok": True,
            "command": command,
        }

    async def subscribe_thread(self, machine_id: str, thread_id: str):
        return self.state_service.subscribe_thread(machine_id, thread_id)

    async def subscribe_machine(self, machine_id: str):
        return self.state_service.subscribe_machine(machine_id)
