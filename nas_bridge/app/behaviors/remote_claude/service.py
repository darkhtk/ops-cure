"""Service layer for the remote_claude behavior.

Sits between the api router (request validation) and the state service
(DB / pub-sub). All `enqueue_*` methods translate browser intents into
queued commands the agent will pick up via /agent/commands/claim.
"""

from __future__ import annotations

import json
from typing import Any

from .state_service import (
    APPROVAL_RESPOND,
    FS_LIST,
    FS_MKDIR,
    RUN_INPUT,
    RUN_INTERRUPT,
    RUN_START,
    SESSION_DELETE,
    SESSION_TRANSCRIPT,
    RemoteClaudeStateService,
    compact_text,
)


class RemoteClaudeBehaviorService:
    def __init__(self, state_service: RemoteClaudeStateService) -> None:
        self.state_service = state_service

    # -------- Machines / Sessions ---------------------------------

    def list_machines(self) -> dict[str, Any]:
        return {"machines": self.state_service.list_machines()}

    def list_sessions(self, machine_id: str, *, limit: int = 200) -> dict[str, Any]:
        return {"sessions": self.state_service.list_sessions(machine_id, limit=limit)}

    def get_session(self, machine_id: str, session_id: str) -> dict[str, Any]:
        session = self.state_service.get_session(machine_id, session_id)
        if session is None:
            raise ValueError("session_not_found")
        return {"session": session}

    # -------- Browser → command queue -----------------------------

    def enqueue_run_start(
        self,
        *,
        machine_id: str,
        cwd: str,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        if machine["status"] != "online":
            raise ValueError("machine_offline")
        payload = {
            "cwd": compact_text(cwd),
            "prompt": prompt,
            "attachments": attachments or [],
        }
        if model: payload["model"] = compact_text(model)
        if permission_mode: payload["permissionMode"] = compact_text(permission_mode)
        command = self.state_service.enqueue_command(
            command_type=RUN_START,
            machine_id=machine_id,
            session_id="",
            requested_by=requested_by,
            prompt=json.dumps(payload),
        )
        return {"ok": True, "command": command}

    def enqueue_run_input(
        self,
        *,
        machine_id: str,
        session_id: str,
        run_id: str | None,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        machine = self.state_service.get_machine(machine_id)
        if machine is None:
            raise ValueError("machine_not_found")
        payload = {"text": text, "attachments": attachments or []}
        command = self.state_service.enqueue_command(
            command_type=RUN_INPUT,
            machine_id=machine_id,
            session_id=session_id,
            run_id=run_id,
            requested_by=requested_by,
            prompt=json.dumps(payload),
        )
        return {"ok": True, "command": command}

    def enqueue_run_interrupt(
        self,
        *,
        machine_id: str,
        session_id: str,
        run_id: str | None,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        command = self.state_service.enqueue_command(
            command_type=RUN_INTERRUPT,
            machine_id=machine_id,
            session_id=session_id,
            run_id=run_id,
            requested_by=requested_by,
        )
        return {"ok": True, "command": command}

    def enqueue_session_delete(
        self,
        *,
        machine_id: str,
        session_id: str,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        # Drop the row immediately so the sidebar reflects the change; the
        # agent unlinks the actual jsonl file when it picks up the command.
        self.state_service.delete_session_record(machine_id, session_id)
        command = self.state_service.enqueue_command(
            command_type=SESSION_DELETE,
            machine_id=machine_id,
            session_id=session_id,
            requested_by=requested_by,
        )
        return {"ok": True, "command": command}

    def enqueue_session_transcript(
        self,
        *,
        machine_id: str,
        session_id: str,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        command = self.state_service.enqueue_command(
            command_type=SESSION_TRANSCRIPT,
            machine_id=machine_id,
            session_id=session_id,
            requested_by=requested_by,
        )
        return {"ok": True, "command": command}

    def enqueue_fs_list(
        self,
        *,
        machine_id: str,
        path: str,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        command = self.state_service.enqueue_command(
            command_type=FS_LIST,
            machine_id=machine_id,
            session_id="",
            requested_by=requested_by,
            prompt=json.dumps({"path": compact_text(path)}),
        )
        return {"ok": True, "command": command}

    def enqueue_fs_mkdir(
        self,
        *,
        machine_id: str,
        parent: str,
        name: str,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        command = self.state_service.enqueue_command(
            command_type=FS_MKDIR,
            machine_id=machine_id,
            session_id="",
            requested_by=requested_by,
            prompt=json.dumps({"parent": compact_text(parent), "name": compact_text(name)}),
        )
        return {"ok": True, "command": command}

    def enqueue_approval_respond(
        self,
        *,
        machine_id: str,
        session_id: str,
        approval_id: str,
        decision: str,
        reason: str | None,
        requested_by: dict[str, Any],
    ) -> dict[str, Any]:
        command = self.state_service.enqueue_command(
            command_type=APPROVAL_RESPOND,
            machine_id=machine_id,
            session_id=session_id,
            requested_by=requested_by,
            prompt=json.dumps({
                "approvalId": approval_id,
                "decision": decision,
                "reason": reason or "",
            }),
        )
        return {"ok": True, "command": command}

    # -------- Agent endpoints --------------------------------------

    def agent_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        machine = payload.get("machine") or {}
        sessions = payload.get("sessions") or []
        if not isinstance(machine, dict) or not machine.get("machineId"):
            raise ValueError("machine_missing")
        machine_record = self.state_service.upsert_machine(machine)
        for s in sessions:
            if isinstance(s, dict):
                try: self.state_service.upsert_session(machine["machineId"], s)
                except Exception: pass
        return {"ok": True, "machine": machine_record, "sessionCount": len(sessions)}

    def agent_claim_command(self, machine_id: str, *, worker_id: str) -> dict[str, Any]:
        return {"command": self.state_service.claim_next_command(machine_id, worker_id=worker_id)}

    def agent_report_command_result(
        self,
        command_id: str,
        *,
        worker_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status == "completed":
            return {"command": self.state_service.complete_command(command_id, worker_id=worker_id, result=result)}
        if status == "failed":
            return {"command": self.state_service.fail_command(command_id, worker_id=worker_id, error=error)}
        raise ValueError(f"unsupported command status: {status}")

    def agent_publish_event(
        self, *, machine_id: str, session_id: str, event: dict[str, Any]
    ) -> None:
        """Forward an event from the agent to the SSE subscribers. The
        agent calls this with each stream-json event from the spawned
        claude process so the browser sees them live."""
        self.state_service.publish_event(machine_id, session_id, event)

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        return self.state_service.get_command_public(command_id)
