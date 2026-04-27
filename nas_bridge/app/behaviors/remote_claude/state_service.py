"""State service for the remote_claude behavior.

Mirrors remote_codex/state_service.py but for the claude CLI's session/run
model. The DB tracks machines, sessions (1 jsonl per session), and a
command queue. A simple in-process pub/sub fans events out to SSE
subscribers without any kernel involvement.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from ...db import session_scope
from ...models import (
    RemoteClaudeCommandModel,
    RemoteClaudeMachineModel,
    RemoteClaudeSessionModel,
)


# Command types
RUN_START = "run.start"
RUN_INPUT = "run.input"
RUN_INTERRUPT = "run.interrupt"
SESSION_DELETE = "session.delete"
FS_LIST = "fs.list"
FS_MKDIR = "fs.mkdir"
APPROVAL_RESPOND = "approval.respond"

# Command statuses
COMMAND_QUEUED = "queued"
COMMAND_RUNNING = "running"
COMMAND_COMPLETED = "completed"
COMMAND_FAILED = "failed"

DEFAULT_DEGRADED_AFTER_SECONDS = 45
DEFAULT_OFFLINE_AFTER_SECONDS = 90


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def compact_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    s = str(value).strip()
    return s if s else default


def loads_json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def dumps_json(value: Any, fallback: str = "{}") -> str:
    try:
        return json.dumps(value)
    except Exception:
        return fallback


class RemoteClaudeStateService:
    """All DB operations + pub/sub. Designed to be created once at app
    startup and shared (it holds an in-memory subscriber list).
    """

    def __init__(self) -> None:
        # session_id → list of asyncio.Queue. Each subscriber is a single
        # SSE stream waiting for events on that session.
        self._session_subs: dict[str, list[Any]] = defaultdict(list)
        self._machine_subs: dict[str, list[Any]] = defaultdict(list)

    # -------- Machines --------------------------------------------------

    def upsert_machine(self, machine: dict[str, Any]) -> dict[str, Any]:
        machine_id = compact_text(machine.get("machineId"))
        if not machine_id:
            raise ValueError("machineId is required")
        with session_scope() as db:
            row = db.get(RemoteClaudeMachineModel, machine_id)
            if row is None:
                row = RemoteClaudeMachineModel(machine_id=machine_id)
                db.add(row)
            row.display_name = compact_text(machine.get("displayName"), machine_id)
            row.source = compact_text(machine.get("source"), "agent")
            caps = machine.get("capabilities") or {}
            row.capabilities_json = dumps_json(caps if isinstance(caps, dict) else {})
            row.last_seen_at = utcnow()
            row.last_sync_at = utcnow()
            row.updated_at = utcnow()
            public = self._machine_row_to_public(row)
        self._publish_machine(machine_id, {"kind": "machine", "machine": public})
        return public

    def list_machines(self) -> list[dict[str, Any]]:
        with session_scope() as db:
            rows = db.execute(select(RemoteClaudeMachineModel)).scalars().all()
            return [self._machine_row_to_public(r) for r in rows]

    def get_machine(self, machine_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.get(RemoteClaudeMachineModel, machine_id)
            return self._machine_row_to_public(row) if row else None

    def _machine_row_to_public(self, row: RemoteClaudeMachineModel) -> dict[str, Any]:
        # Status derives from heartbeat freshness — same rule as remote_codex.
        now = datetime.now(timezone.utc)
        last_seen = row.last_seen_at
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        delta = (now - last_seen).total_seconds() if last_seen else 9_999
        if delta < DEFAULT_DEGRADED_AFTER_SECONDS:
            status = "online"
        elif delta < DEFAULT_OFFLINE_AFTER_SECONDS:
            status = "degraded"
        else:
            status = "offline"
        return {
            "machineId": row.machine_id,
            "displayName": row.display_name,
            "status": status,
            "source": row.source,
            "capabilities": loads_json(row.capabilities_json, {}),
            "lastSeenAt": isoformat(row.last_seen_at),
            "lastSyncAt": isoformat(row.last_sync_at),
        }

    # -------- Sessions --------------------------------------------------

    def upsert_session(self, machine_id: str, session: dict[str, Any]) -> dict[str, Any]:
        session_id = compact_text(session.get("sessionId"))
        if not session_id:
            raise ValueError("sessionId is required")
        with session_scope() as db:
            row = db.execute(
                select(RemoteClaudeSessionModel).where(
                    RemoteClaudeSessionModel.machine_id == machine_id,
                    RemoteClaudeSessionModel.session_id == session_id,
                )
            ).scalar_one_or_none()
            if row is None:
                row = RemoteClaudeSessionModel(machine_id=machine_id, session_id=session_id)
                db.add(row)
            row.title = compact_text(session.get("title"), "(no preview)")
            row.cwd = compact_text(session.get("cwd"))
            row.jsonl_path = compact_text(session.get("jsonlPath"))
            row.updated_at_ms = int(session.get("updatedAtMs") or 0)
            row.created_at_ms = int(session.get("createdAtMs") or 0)
            row.first_user_message = compact_text(session.get("firstUserMessage"))
            row.event_count = int(session.get("eventCount") or 0)
            row.file_size = int(session.get("fileSize") or 0)
            via = compact_text(session.get("via"), "cli")
            if via in ("web", "cli"):
                # Sticky upgrade: never overwrite "web" with "cli". The
                # agent's in-memory web-session set vanishes across
                # restarts, but the browser-origin attribution is meant to
                # be permanent for the life of the session row.
                if not (row.via == "web" and via == "cli"):
                    row.via = via
            row.synced_at = utcnow()
            row.updated_at = utcnow()
            public = self._session_row_to_public(row)
        self._publish_machine(machine_id, {"kind": "session", "session": public})
        return public

    def list_sessions(self, machine_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with session_scope() as db:
            rows = (
                db.execute(
                    select(RemoteClaudeSessionModel)
                    .where(RemoteClaudeSessionModel.machine_id == machine_id)
                    .order_by(RemoteClaudeSessionModel.updated_at_ms.desc())
                    .limit(max(1, int(limit)))
                )
                .scalars()
                .all()
            )
            return [self._session_row_to_public(r) for r in rows]

    def get_session(self, machine_id: str, session_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.execute(
                select(RemoteClaudeSessionModel).where(
                    RemoteClaudeSessionModel.machine_id == machine_id,
                    RemoteClaudeSessionModel.session_id == session_id,
                )
            ).scalar_one_or_none()
            return self._session_row_to_public(row) if row else None

    def delete_session_record(self, machine_id: str, session_id: str) -> bool:
        with session_scope() as db:
            row = db.execute(
                select(RemoteClaudeSessionModel).where(
                    RemoteClaudeSessionModel.machine_id == machine_id,
                    RemoteClaudeSessionModel.session_id == session_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            db.delete(row)
        return True

    def _session_row_to_public(self, row: RemoteClaudeSessionModel) -> dict[str, Any]:
        return {
            "sessionId": row.session_id,
            "machineId": row.machine_id,
            "title": row.title,
            "cwd": row.cwd,
            "updatedAtMs": row.updated_at_ms,
            "createdAtMs": row.created_at_ms,
            "firstUserMessage": row.first_user_message,
            "eventCount": row.event_count,
            "fileSize": row.file_size,
            "via": row.via,
            "syncedAt": isoformat(row.synced_at),
        }

    # -------- Commands --------------------------------------------------

    def enqueue_command(
        self,
        *,
        command_type: str,
        machine_id: str,
        session_id: str = "",
        run_id: str | None = None,
        prompt: str | None = None,
        requested_by: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with session_scope() as db:
            row = RemoteClaudeCommandModel(
                type=command_type,
                status=COMMAND_QUEUED,
                machine_id=machine_id,
                session_id=session_id,
                run_id=run_id,
                prompt=prompt,
                requested_by_json=dumps_json(requested_by or {}),
            )
            db.add(row)
            db.flush()
            public = self._command_row_to_public(row)
        self._publish_session(machine_id, session_id, {"kind": "command", "command": public})
        return public

    def claim_next_command(self, machine_id: str, *, worker_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.execute(
                select(RemoteClaudeCommandModel)
                .where(
                    RemoteClaudeCommandModel.machine_id == machine_id,
                    RemoteClaudeCommandModel.status == COMMAND_QUEUED,
                )
                .order_by(RemoteClaudeCommandModel.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = COMMAND_RUNNING
            row.worker_id = compact_text(worker_id, "unknown-worker")
            row.started_at = utcnow()
            row.updated_at = row.started_at
            db.flush()
            public = self._command_row_to_public(row)
        self._publish_session(public["machineId"], public["sessionId"], {"kind": "command", "command": public})
        return public

    def complete_command(
        self, command_id: str, *, worker_id: str, result: dict[str, Any] | None
    ) -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(RemoteClaudeCommandModel, command_id)
            if row is None:
                raise ValueError(f"Unknown command: {command_id}")
            row.status = COMMAND_COMPLETED
            row.worker_id = compact_text(worker_id, row.worker_id or "")
            row.result_json = dumps_json(result or {})
            row.completed_at = utcnow()
            row.updated_at = row.completed_at
            db.flush()
            public = self._command_row_to_public(row)
        self._publish_session(public["machineId"], public["sessionId"], {"kind": "command", "command": public})
        return public

    def fail_command(
        self, command_id: str, *, worker_id: str, error: dict[str, Any] | None
    ) -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(RemoteClaudeCommandModel, command_id)
            if row is None:
                raise ValueError(f"Unknown command: {command_id}")
            row.status = COMMAND_FAILED
            row.worker_id = compact_text(worker_id, row.worker_id or "")
            row.error_json = dumps_json(error or {})
            row.completed_at = utcnow()
            row.updated_at = row.completed_at
            db.flush()
            public = self._command_row_to_public(row)
        self._publish_session(public["machineId"], public["sessionId"], {"kind": "command", "command": public})
        return public

    def get_command_public(self, command_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.get(RemoteClaudeCommandModel, command_id)
            return self._command_row_to_public(row) if row else None

    def _command_row_to_public(self, row: RemoteClaudeCommandModel) -> dict[str, Any]:
        return {
            "id": row.command_id,
            "commandId": row.command_id,
            "type": row.type,
            "status": row.status,
            "machineId": row.machine_id,
            "sessionId": row.session_id,
            "runId": row.run_id,
            "prompt": row.prompt,
            "requestedBy": loads_json(row.requested_by_json, {}),
            "result": loads_json(row.result_json, None),
            "error": loads_json(row.error_json, None),
            "workerId": row.worker_id,
            "createdAt": isoformat(row.created_at),
            "updatedAt": isoformat(row.updated_at),
            "startedAt": isoformat(row.started_at),
            "completedAt": isoformat(row.completed_at),
        }

    # -------- Pub/sub for SSE -----------------------------------------

    def subscribe_session(self, machine_id: str, session_id: str):
        return self._SubscriptionContext(self._session_subs, f"{machine_id}|{session_id}")

    def subscribe_machine(self, machine_id: str):
        return self._SubscriptionContext(self._machine_subs, machine_id)

    def _publish_session(self, machine_id: str, session_id: str, event: dict[str, Any]) -> None:
        key = f"{machine_id}|{session_id}"
        for queue in list(self._session_subs.get(key, ())):
            try: queue.put_nowait(event)
            except Exception: pass
        # Mirror command lifecycle events to machine-level subscribers so the
        # browser can wait on fs.list / fs.mkdir / session.start results via
        # one machine SSE instead of polling /commands/{id}. Other event kinds
        # (stream-json messages, etc.) stay session-scoped to avoid blasting
        # every browser subscribed to the machine with per-message traffic.
        if event.get("kind") == "command":
            self._publish_machine(machine_id, event)

    def _publish_machine(self, machine_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._machine_subs.get(machine_id, ())):
            try: queue.put_nowait(event)
            except Exception: pass

    def publish_event(self, machine_id: str, session_id: str, event: dict[str, Any]) -> None:
        """Bridge entrypoint for pushing arbitrary events (e.g. claude
        stream-json events forwarded by the agent) onto the SSE stream.
        """
        self._publish_session(machine_id, session_id, event)

    class _SubscriptionContext:
        def __init__(self, registry: dict, key: str) -> None:
            self._registry = registry
            self._key = key
            self._queue = None

        async def __aenter__(self):
            import asyncio
            self._queue = asyncio.Queue()
            self._registry[self._key].append(self._queue)
            return self._queue

        async def __aexit__(self, exc_type, exc, tb):
            try:
                self._registry[self._key].remove(self._queue)
            except (ValueError, KeyError):
                pass
            return False
