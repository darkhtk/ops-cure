"""State service for the remote_claude behavior.

Mirrors remote_codex/state_service.py but for the claude CLI's session/run
model. The DB tracks machines, sessions (1 jsonl per session), and a
command queue. A simple in-process pub/sub fans events out to SSE
subscribers without any kernel involvement.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from ...db import session_scope
from ...kernel.events import EventEnvelope, EventSummary, encode_event_cursor
from ...models import (
    RemoteClaudeCommandModel,
    RemoteClaudeMachineModel,
    RemoteClaudeSessionModel,
)


REMOTE_CLAUDE_MACHINE_SPACE_PREFIX = "remote_claude.machine:"
REMOTE_CLAUDE_SESSION_SPACE_PREFIX = "remote_claude.session:"


def remote_claude_machine_space_id(machine_id: str) -> str:
    """Synthetic kernel space_id for machine-scoped events (command lifecycle,
    session list updates, machine status). Lets browsers + agents subscribe
    via the generic /api/events/spaces/{space_id}/stream channel."""
    return f"{REMOTE_CLAUDE_MACHINE_SPACE_PREFIX}{machine_id}"


def remote_claude_session_space_id(session_id: str) -> str:
    """Synthetic kernel space_id for session-scoped stream-json events."""
    return f"{REMOTE_CLAUDE_SESSION_SPACE_PREFIX}{session_id}"


# Command types
RUN_START = "run.start"
RUN_INPUT = "run.input"
RUN_INTERRUPT = "run.interrupt"
SESSION_DELETE = "session.delete"
SESSION_TRANSCRIPT = "session.transcript"
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

    def __init__(self, *, kernel_subscription_broker: Any | None = None) -> None:
        # _machine_subs is still used internally by /sessions/{sid}/transcript
        # to wait for the agent's session.transcript command to complete.
        # _session_subs is unused (the legacy /sessions/{sid}/live endpoint
        # was removed in Phase 5) but kept as an empty dict to preserve the
        # call-shape of _publish_session for any future internal consumer.
        self._session_subs: dict[str, list[Any]] = defaultdict(list)
        self._machine_subs: dict[str, list[Any]] = defaultdict(list)
        # Kernel broker. Every event is mirrored onto the generic
        # /api/events/spaces/.../stream channel so the codex/chat/ops
        # behaviors all share the same transport. Broker has its own
        # 1024-event backlog per space + cursor-based replay, so we no
        # longer need a behavior-local ring buffer.
        self._kernel_subscription_broker = kernel_subscription_broker

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

    def subscribe_machine(self, machine_id: str):
        # Internal-only: used by /sessions/{sid}/transcript to await the
        # agent's session.transcript command result. Not exposed via SSE
        # anymore (see Phase 5 of the kernel-broker migration).
        return self._SubscriptionContext(self._machine_subs, machine_id)

    def _publish_session(self, machine_id: str, session_id: str, event: dict[str, Any]) -> None:
        key = f"{machine_id}|{session_id}"
        for queue in list(self._session_subs.get(key, ())):
            try: queue.put_nowait(event)
            except Exception: pass
        # Mirror command lifecycle events to internal _machine_subs (used by
        # the /sessions/{sid}/transcript endpoint to await the agent's
        # session.transcript command). Suppress the broker re-mirror -- the
        # kernel publish happens once, here, with the right space.
        if event.get("kind") == "command":
            self._publish_machine(machine_id, event, _suppress_mirror=True)
        # Kernel broker mirror: command -> machine space; stream-json
        # (claude.event etc.) -> session space.
        self._mirror_to_kernel_broker(machine_id=machine_id, session_id=session_id, payload=event)

    def _publish_machine(self, machine_id: str, event: dict[str, Any], *, _suppress_mirror: bool = False) -> None:
        for queue in list(self._machine_subs.get(machine_id, ())):
            try: queue.put_nowait(event)
            except Exception: pass
        if not _suppress_mirror:
            self._mirror_to_kernel_broker(machine_id=machine_id, session_id="", payload=event)

    def _mirror_to_kernel_broker(self, *, machine_id: str, session_id: str, payload: dict[str, Any]) -> None:
        """Publish the same payload onto the kernel subscription broker so
        subscribers on /api/events/spaces/.../stream see exactly what the
        legacy /api/remote-claude/.../live SSE pipes carry. Best-effort:
        broker dispatch failures must not destabilize the legacy publish.
        """
        broker = self._kernel_subscription_broker
        if broker is None:
            return
        kind = payload.get("kind") or "event"
        # Pick the target space + event id + actor based on payload kind.
        if kind == "command":
            command = payload.get("command") or {}
            event_id = compact_text(command.get("commandId")) or compact_text(machine_id) or "remote_claude.command"
            status = compact_text(command.get("status"))
            event_kind = f"remote_claude.command.{status}" if status else "remote_claude.command"
            actor = compact_text(command.get("workerId")) or compact_text(machine_id) or "remote_claude"
            space_id = remote_claude_machine_space_id(machine_id)
        elif kind == "session":
            session = payload.get("session") or {}
            sid = compact_text(session.get("sessionId")) or session_id or machine_id
            event_id = sid or "remote_claude.session"
            event_kind = "remote_claude.session"
            actor = compact_text(machine_id) or "remote_claude"
            space_id = remote_claude_machine_space_id(machine_id)
        elif kind == "machine":
            event_id = compact_text(machine_id) or "remote_claude.machine"
            event_kind = "remote_claude.machine"
            actor = compact_text(machine_id) or "remote_claude"
            space_id = remote_claude_machine_space_id(machine_id)
        else:
            # claude.event / claude.stderr / claude.exit / claude.parse_error:
            # session-scoped stream-json. If session_id is empty (orphan /
            # diagnostic event), drop — there's no useful target space.
            if not session_id:
                return
            inner = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            inner_id = compact_text(inner.get("uuid")) or compact_text(inner.get("id"))
            event_id = inner_id or f"{session_id}-{int(time.time() * 1_000_000)}"
            event_kind = kind
            actor = compact_text(machine_id) or "remote_claude"
            space_id = remote_claude_session_space_id(session_id)

        created_at = utcnow()
        try:
            envelope = EventEnvelope(
                cursor=encode_event_cursor(created_at=created_at, event_id=event_id),
                space_id=space_id,
                event=EventSummary(
                    id=event_id,
                    kind=event_kind,
                    actor_name=actor,
                    content=json.dumps(payload, ensure_ascii=False),
                    created_at=created_at,
                ),
            )
            broker.publish(space_id=space_id, item=envelope)
        except Exception:  # noqa: BLE001
            # Broker dispatch failures must not break the legacy publish.
            pass

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
