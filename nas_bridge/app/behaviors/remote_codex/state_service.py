from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import delete, func, select

from ...db import session_scope
from ...kernel.events import EventEnvelope, EventSummary, encode_event_cursor
from ...kernel.tasks import (
    KernelTaskService,
    TASK_STATUS_CLAIMED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
)
from ...models import (
    KernelTaskModel,
    RemoteCodexCommandModel,
    RemoteCodexMachineModel,
    RemoteCodexMessageModel,
    RemoteCodexThreadModel,
    RemoteTaskModel,
)

DEFAULT_DEGRADED_AFTER_SECONDS = 45
DEFAULT_OFFLINE_AFTER_SECONDS = 90
DEFAULT_STORED_MESSAGE_WINDOW = 200
DEFAULT_THREAD_MESSAGE_LIMIT = 120
DEFAULT_PENDING_COMMAND_VISIBILITY_SECONDS = 180
ACTIVE_PENDING_TASK_STATUSES = {"queued", "claimed", "executing", "verifying", "blocked_approval"}
PENDING_COMMAND_TURN_STATUSES = {"queued", "inprogress", "in_progress", "running"}
COMMAND_QUEUED = "queued"
COMMAND_RUNNING = "running"
COMMAND_COMPLETED = "completed"
COMMAND_FAILED = "failed"
TURN_START = "turn.start"
TURN_INTERRUPT = "turn.interrupt"
THREAD_DELETE = "thread.delete"
THREAD_START = "thread.start"
FS_LIST = "fs.list"
FS_MKDIR = "fs.mkdir"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    next_value = ensure_utc(value)
    return next_value.isoformat() if next_value is not None else None


def compact_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    normalized = " ".join(text.split()).strip()
    return normalized or fallback


def coerce_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def dumps_json(value: Any, fallback: str = "{}") -> str:
    if value is None:
        return fallback
    return json.dumps(value, ensure_ascii=False)


def loads_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def serialize_maybe_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return text if text.startswith("{") or text.startswith("[") else dumps_json(text, fallback='""')
    return dumps_json(value)


def restore_maybe_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def compute_machine_status(
    last_seen_at: datetime | None,
    *,
    degraded_after_seconds: int = DEFAULT_DEGRADED_AFTER_SECONDS,
    offline_after_seconds: int = DEFAULT_OFFLINE_AFTER_SECONDS,
) -> str:
    next_seen_at = ensure_utc(last_seen_at)
    if next_seen_at is None:
        return "offline"
    age_seconds = max(0.0, (utcnow() - next_seen_at).total_seconds())
    if age_seconds > offline_after_seconds:
        return "offline"
    if age_seconds > degraded_after_seconds:
        return "degraded"
    return "online"


def normalize_requested_by(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "authMethod": compact_text(payload.get("authMethod") or payload.get("auth_method"), "unknown"),
        "email": compact_text(payload.get("email")) or None,
        "name": compact_text(payload.get("name")) or None,
        "subject": compact_text(payload.get("subject")) or None,
        "assertedClientId": compact_text(
            payload.get("assertedClientId") or payload.get("asserted_client_id")
        )
        or None,
    }


def normalize_command_attachment(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = compact_text(value.get("name"), "attachment")
    mime_type = compact_text(value.get("mimeType") or value.get("mime_type"), "application/octet-stream")
    kind = compact_text(value.get("kind")).lower()
    if not kind:
        kind = "image" if mime_type.startswith("image/") else "file"
    if kind not in {"image", "file"}:
        return None
    size = max(0, coerce_int(value.get("size"), 0))
    normalized = {
        "name": name,
        "mimeType": mime_type,
        "kind": kind,
        "size": size,
    }
    if kind == "image":
        data_url = compact_text(value.get("dataUrl") or value.get("data_url"))
        if not data_url:
            return None
        normalized["dataUrl"] = data_url
        return normalized
    bytes_base64 = compact_text(value.get("bytesBase64") or value.get("bytes_base64"))
    if not bytes_base64:
        return None
    normalized["bytesBase64"] = bytes_base64
    return normalized


def normalize_command_attachments(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = payload or {}
    raw = payload.get("attachments") or payload.get("browserAttachments") or []
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        attachment = normalize_command_attachment(item)
        if attachment is not None:
            normalized.append(attachment)
    return normalized


def serialize_requested_by(payload: dict[str, Any] | None = None) -> str:
    normalized = normalize_requested_by(payload)
    attachments = normalize_command_attachments(payload)
    if attachments:
        normalized["attachments"] = attachments
    return dumps_json(normalized)


def public_command_attachments(
    payload: dict[str, Any] | None,
    *,
    include_attachment_data: bool = False,
) -> list[dict[str, Any]]:
    attachments = normalize_command_attachments(payload)
    public: list[dict[str, Any]] = []
    for attachment in attachments:
        item = {
            "name": attachment["name"],
            "mimeType": attachment["mimeType"],
            "kind": attachment["kind"],
            "size": attachment["size"],
        }
        if include_attachment_data:
            if attachment["kind"] == "image":
                item["dataUrl"] = attachment.get("dataUrl")
            else:
                item["bytesBase64"] = attachment.get("bytesBase64")
        public.append(item)
    return public


def prompt_preview(prompt: str | None, max_length: int = 160) -> str | None:
    text = compact_text(prompt)
    if not text:
        return None
    return text if len(text) <= max_length else f"{text[: max_length - 3]}..."


def normalize_message_text(value: Any) -> str:
    return compact_text(value).casefold()


def command_turn_status_from_result(result: Any) -> str:
    if isinstance(result, dict):
        return compact_text(result.get("turnStatus")).lower()
    return ""


def command_turn_id_from_row(row: RemoteCodexCommandModel) -> str | None:
    turn_id = compact_text(row.turn_id) or None
    if turn_id:
        return turn_id
    result = loads_json(row.result_json, None)
    if isinstance(result, dict):
        next_turn_id = compact_text(result.get("turnId")) or None
        if next_turn_id:
            return next_turn_id
    return None


@dataclass(slots=True)
class SubscriptionHandle:
    queue: asyncio.Queue[dict[str, Any]]
    unsubscribe: Callable[[], None]


REMOTE_CODEX_MACHINE_SPACE_PREFIX = "remote_codex.machine:"


def remote_codex_machine_space_id(machine_id: str) -> str:
    """Synthetic kernel ``space_id`` used to publish machine-scoped command
    events into the kernel subscription broker. Lets device-side runners
    subscribe via the existing ``/api/events/spaces/{space_id}/stream`` SSE
    endpoint without inventing a new transport just for this behavior.
    """
    return f"{REMOTE_CODEX_MACHINE_SPACE_PREFIX}{machine_id}"


REMOTE_CODEX_COMMAND_KIND = "remote_codex.command"


class RemoteCodexStateService:
    def __init__(
        self,
        *,
        kernel_subscription_broker: Any | None = None,
        kernel_task_service: KernelTaskService | None = None,
    ) -> None:
        self._subscribers: dict[tuple[str, str], dict[str, asyncio.Queue[dict[str, Any]]]] = defaultdict(dict)
        # Optional kernel-level broker so machine-scoped command events also
        # flow out through the generic /api/events/... pipe. Behavior is a
        # pure superset: when this is wired up, kernel subscribers see the
        # same payload the legacy /api/remote-codex/.../live SSE was already
        # publishing; when None, only the legacy pipe runs (today's path).
        self._kernel_subscription_broker = kernel_subscription_broker
        # Optional kernel task service so the legacy ``remote_codex_commands``
        # writes also create / transition a generic ``KernelTaskModel`` row
        # keyed off the same primary key. Mirror is best-effort during the
        # migration window — failures must not destabilize the legacy flow.
        self._kernel_task_service = kernel_task_service or KernelTaskService()

    def _subscribe(self, machine_id: str, thread_id: str) -> SubscriptionHandle:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        subscription_id = str(uuid.uuid4())
        key = (machine_id, thread_id)
        self._subscribers[key][subscription_id] = queue

        def unsubscribe() -> None:
            self._subscribers[key].pop(subscription_id, None)
            if not self._subscribers[key]:
                self._subscribers.pop(key, None)

        return SubscriptionHandle(queue=queue, unsubscribe=unsubscribe)

    def subscribe_thread(self, machine_id: str, thread_id: str) -> SubscriptionHandle:
        return self._subscribe(machine_id, thread_id)

    def subscribe_machine(self, machine_id: str) -> SubscriptionHandle:
        return self._subscribe(machine_id, "*")

    def _mirror_to_kernel_broker(
        self,
        machine_id: str,
        thread_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Forward command-related publishes onto the generic kernel
        subscription broker so device-side runners can use the regular
        ``/api/events/spaces/.../stream`` SSE channel instead of polling
        ``claim-next`` / ``agent/sync`` in a hot loop. We only mirror
        ``command`` events for now — that's what runners need to wake on.
        """
        broker = self._kernel_subscription_broker
        if broker is None:
            return
        if payload.get("kind") != "command":
            return
        command = payload.get("command")
        if not isinstance(command, dict):
            return

        command_id = compact_text(command.get("commandId"))
        if not command_id:
            return
        status = compact_text(command.get("status"))
        kind = f"remote_codex.command.{status}" if status else "remote_codex.command"
        actor_name = compact_text(command.get("workerId")) or compact_text(machine_id) or "remote_codex"
        space_id = remote_codex_machine_space_id(machine_id)
        created_at = utcnow()
        envelope = EventEnvelope(
            cursor=encode_event_cursor(created_at=created_at, event_id=command_id),
            space_id=space_id,
            event=EventSummary(
                id=command_id,
                kind=kind,
                actor_name=actor_name,
                content=json.dumps(
                    {
                        "machineId": machine_id,
                        "threadId": thread_id,
                        "command": command,
                    },
                    ensure_ascii=False,
                ),
                created_at=created_at,
            ),
        )
        try:
            broker.publish(space_id=space_id, item=envelope)
        except Exception:  # noqa: BLE001 — broker dispatch failures must not break the legacy publish
            pass

    def _mirror_command_enqueue(
        self,
        db,
        *,
        row: RemoteCodexCommandModel,
        command_type: str,
    ) -> None:
        """Mirror a freshly-enqueued remote_codex command into a generic
        ``KernelTaskModel`` row keyed off the command id. The kernel side
        is best-effort during the migration window — failures are
        swallowed so the legacy enqueue stays the source of truth.
        """
        try:
            self._kernel_task_service.enqueue(
                db,
                space_id=remote_codex_machine_space_id(row.machine_id),
                kind=REMOTE_CODEX_COMMAND_KIND,
                payload={
                    "machine_id": row.machine_id,
                    "thread_id": row.thread_id,
                    "command_type": command_type,
                    "turn_id": row.turn_id,
                    "task_id": row.task_id,
                },
                requested_by=compact_text(loads_json(row.requested_by_json, {}).get("actorId") or ""),
                task_id=row.command_id,
            )
        except Exception:  # noqa: BLE001 — mirror must not destabilize the legacy write
            pass

    def _mirror_command_transition(
        self,
        db,
        *,
        command_id: str,
        status: str,
        owner_actor_id: str | None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Drive the mirrored kernel task row to the same terminal /
        intermediate status as the legacy command row, bypassing lease
        semantics: the legacy worker_id field already enforces single-
        ownership, so the kernel mirror just shadows the status, owner,
        and result/error blob without an extra lease handshake.
        """
        try:
            kernel_row = db.get(KernelTaskModel, command_id)
            if kernel_row is None:
                return
            now = utcnow()
            kernel_row.status = status
            if owner_actor_id is not None:
                kernel_row.owner_actor_id = owner_actor_id
            if status == TASK_STATUS_CLAIMED:
                kernel_row.claim_count = int(kernel_row.claim_count or 0) + 1
                kernel_row.started_at = kernel_row.started_at or now
            if status == TASK_STATUS_COMPLETED and result is not None:
                kernel_row.result_json = dumps_json(result)
                kernel_row.completed_at = now
                kernel_row.lease_expires_at = None
            elif status == TASK_STATUS_FAILED:
                kernel_row.error_json = dumps_json(error or {"message": "Unknown error"})
                kernel_row.completed_at = now
                kernel_row.lease_expires_at = None
            kernel_row.updated_at = now
            db.flush()
        except Exception:  # noqa: BLE001 — mirror must not destabilize the legacy write
            pass

    def _publish(self, machine_id: str, thread_id: str, payload: dict[str, Any]) -> None:
        queues: list[asyncio.Queue[dict[str, Any]]] = []
        if thread_id == "*":
            for (next_machine_id, _next_thread_id), subscribers in list(self._subscribers.items()):
                if next_machine_id != machine_id:
                    continue
                queues.extend(list(subscribers.values()))
        else:
            queues.extend(list(self._subscribers.get((machine_id, thread_id), {}).values()))
            queues.extend(list(self._subscribers.get((machine_id, "*"), {}).values()))

        self._mirror_to_kernel_broker(machine_id, thread_id, payload)

        seen_queue_ids: set[int] = set()
        for queue in queues:
            queue_id = id(queue)
            if queue_id in seen_queue_ids:
                continue
            seen_queue_ids.add(queue_id)
            queue.put_nowait(payload)

    def _machine_row_to_public(self, row: RemoteCodexMachineModel, *, thread_count: int | None = None) -> dict[str, Any]:
        return {
            "machineId": row.machine_id,
            "displayName": row.display_name,
            "source": row.source or "agent",
            "status": compute_machine_status(row.last_seen_at),
            "activeTransport": row.active_transport or "filesystem-storage",
            "runtimeMode": row.runtime_mode or "filesystem-readonly",
            "runtimeAvailable": bool(row.runtime_available),
            "capabilities": loads_json(row.capabilities_json, {}),
            "runtimeDescriptor": restore_maybe_json(row.runtime_descriptor_json),
            "lastSeenAt": isoformat(row.last_seen_at),
            "lastSyncAt": isoformat(row.last_sync_at),
            "lastRuntimeError": row.last_runtime_error,
            "lastDiagnostic": row.last_diagnostic,
            "threadCount": thread_count if thread_count is not None else 0,
        }

    def _thread_row_to_public(self, row: RemoteCodexThreadModel) -> dict[str, Any]:
        return {
            "id": row.thread_id,
            "title": row.title or "(untitled)",
            "cwd": row.cwd or "",
            "rolloutPath": row.rollout_path or "",
            "updatedAtMs": row.updated_at_ms or 0,
            "createdAtMs": row.created_at_ms or 0,
            "source": row.source,
            "modelProvider": row.model_provider,
            "model": row.model,
            "reasoningEffort": row.reasoning_effort,
            "cliVersion": row.cli_version,
            "firstUserMessage": row.first_user_message or "",
            "forkedFromId": row.forked_from_id,
            "ephemeral": bool(row.ephemeral),
            "status": restore_maybe_json(row.status_json),
            "agentNickname": row.agent_nickname,
            "agentRole": row.agent_role,
        }

    def _message_row_to_public(self, row: RemoteCodexMessageModel) -> dict[str, Any]:
        return {
            "lineNumber": row.line_number,
            "timestamp": row.timestamp,
            "role": row.role,
            "phase": row.phase,
            "text": row.text,
            "images": loads_json(row.images_json, []),
        }

    def get_command_public(self, command_id: str) -> dict[str, Any] | None:
        """Public shape of a command by id, or None if not found. Used for
        browser polling of fs.list / fs.mkdir / thread.start results.
        Tries to map the public field name {commandId, status, result, ...}
        to the legacy {id, status, result, ...} field set the browser uses.
        """
        with session_scope() as db:
            row = db.get(RemoteCodexCommandModel, command_id)
            if row is None:
                return None
            public = self._command_row_to_public(row)
        # Normalize the id field — _command_row_to_public uses "commandId"
        # but the rest of the codex-remote browser uses {id, status, ...}.
        return {**public, "id": public.get("commandId")}

    def _command_row_to_public(
        self,
        row: RemoteCodexCommandModel,
        *,
        include_attachment_data: bool = False,
    ) -> dict[str, Any]:
        result = loads_json(row.result_json, None)
        error = loads_json(row.error_json, None)
        turn_id = command_turn_id_from_row(row)
        requested_by = loads_json(row.requested_by_json, {})
        return {
            "commandId": row.command_id,
            "type": row.type,
            "status": row.status,
            "machineId": row.machine_id,
            "threadId": row.thread_id,
            "taskId": row.task_id,
            "turnId": turn_id,
            "prompt": row.prompt,
            "promptPreview": prompt_preview(row.prompt),
            "createdAt": isoformat(row.created_at),
            "updatedAt": isoformat(row.updated_at),
            "startedAt": isoformat(row.started_at),
            "completedAt": isoformat(row.completed_at),
            "requestedBy": normalize_requested_by(requested_by),
            "attachments": public_command_attachments(
                requested_by,
                include_attachment_data=include_attachment_data,
            ),
            "result": result,
            "error": error,
        }

    def _command_row_to_pending_message(
        self,
        row: RemoteCodexCommandModel,
        *,
        recent_user_texts: set[str],
        task_status_by_id: dict[str, str],
    ) -> dict[str, Any] | None:
        if row.type != TURN_START:
            return None

        prompt = compact_text(row.prompt)
        if not prompt:
            return None

        normalized_prompt = normalize_message_text(prompt)
        if normalized_prompt in recent_user_texts:
            return None

        status = compact_text(row.status).lower()
        result = loads_json(row.result_json, None)
        turn_status = command_turn_status_from_result(result)

        created_at = ensure_utc(row.created_at) or utcnow()
        age_seconds = max(0.0, (utcnow() - created_at).total_seconds())
        is_fresh_pending = age_seconds <= DEFAULT_PENDING_COMMAND_VISIBILITY_SECONDS
        linked_task_status = compact_text(task_status_by_id.get(row.task_id)).lower() if row.task_id else ""
        linked_task_active = linked_task_status in ACTIVE_PENDING_TASK_STATUSES

        should_surface = (status in {COMMAND_QUEUED, COMMAND_RUNNING} and (is_fresh_pending or linked_task_active)) or (
            status == COMMAND_COMPLETED
            and turn_status in PENDING_COMMAND_TURN_STATUSES
            and (is_fresh_pending or linked_task_active)
        )
        if not should_surface:
            return None

        requested_by = loads_json(row.requested_by_json, {})
        attachments = normalize_command_attachments(requested_by)
        images = [
            {
                "src": attachment["dataUrl"],
                "alt": attachment["name"],
                "title": attachment["name"],
            }
            for attachment in attachments
            if attachment.get("kind") == "image" and compact_text(attachment.get("dataUrl"))
        ]

        synthetic_line_number = -max(1, int(created_at.timestamp() * 1000))
        recent_user_texts.add(normalized_prompt)
        return {
            "lineNumber": synthetic_line_number,
            "timestamp": isoformat(created_at),
            "role": "user",
            "phase": None,
            "text": prompt,
            "images": images,
        }

    def _sort_public_machines(self, machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rank = {"online": 0, "degraded": 1, "offline": 2}
        return sorted(
            machines,
            key=lambda machine: (
                rank.get(machine.get("status"), 99),
                -(ensure_utc(datetime.fromisoformat(machine["lastSeenAt"])).timestamp() if machine.get("lastSeenAt") else 0),
            ),
        )

    def _stale_thread_command_ids(
        self,
        db,
        machine_id: str,
        thread_id: str,
    ) -> tuple[set[str], set[str]]:
        rows = list(
            db.scalars(
                select(RemoteCodexCommandModel)
                .where(
                    RemoteCodexCommandModel.machine_id == machine_id,
                    RemoteCodexCommandModel.thread_id == thread_id,
                )
                .order_by(RemoteCodexCommandModel.updated_at.desc(), RemoteCodexCommandModel.created_at.desc())
            )
        )
        if not rows:
            return set(), set()

        latest_turn_id = None
        for row in rows:
            if compact_text(row.type).lower() != TURN_START:
                continue
            if compact_text(row.status).lower() != COMMAND_COMPLETED:
                continue
            latest_turn_id = command_turn_id_from_row(row)
            if latest_turn_id:
                break

        task_ids = sorted({row.task_id for row in rows if row.task_id})
        task_status_by_id: dict[str, str] = {}
        if task_ids:
            task_status_by_id = {
                row.id: compact_text(row.status).lower()
                for row in db.scalars(select(RemoteTaskModel).where(RemoteTaskModel.id.in_(task_ids)))
            }

        now = utcnow()
        stale_command_ids: set[str] = set()
        deletable_command_ids: set[str] = set()
        for row in rows:
            if compact_text(row.type).lower() != TURN_START:
                continue
            if compact_text(row.status).lower() != COMMAND_COMPLETED:
                continue

            turn_status = command_turn_status_from_result(loads_json(row.result_json, None))
            if turn_status not in PENDING_COMMAND_TURN_STATUSES:
                continue

            command_turn_id = command_turn_id_from_row(row)
            linked_task_status = compact_text(task_status_by_id.get(row.task_id)).lower() if row.task_id else ""
            linked_task_active = linked_task_status in ACTIVE_PENDING_TASK_STATUSES
            created_at = ensure_utc(row.created_at) or now
            age_seconds = max(0.0, (now - created_at).total_seconds())

            superseded_by_newer_turn = bool(
                latest_turn_id and command_turn_id and command_turn_id != latest_turn_id
            )
            expired_without_active_task = not linked_task_active and age_seconds > DEFAULT_PENDING_COMMAND_VISIBILITY_SECONDS
            if superseded_by_newer_turn or expired_without_active_task:
                stale_command_ids.add(row.command_id)
                if not linked_task_active:
                    deletable_command_ids.add(row.command_id)

        return stale_command_ids, deletable_command_ids

    def get_machine_summary(self, *, active_only: bool = True) -> dict[str, int]:
        machines = self.list_machines(active_only=active_only)
        return {
            "totalMachines": len(machines),
            "onlineMachines": len([machine for machine in machines if machine["status"] == "online"]),
            "degradedMachines": len([machine for machine in machines if machine["status"] == "degraded"]),
            "offlineMachines": len([machine for machine in machines if machine["status"] == "offline"]),
        }

    def list_machines(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        with session_scope() as db:
            thread_counts = dict(
                db.execute(
                    select(RemoteCodexThreadModel.machine_id, func.count(RemoteCodexThreadModel.id))
                    .group_by(RemoteCodexThreadModel.machine_id)
                ).all()
            )
            rows = list(db.scalars(select(RemoteCodexMachineModel)))
            machines = [
                self._machine_row_to_public(row, thread_count=thread_counts.get(row.machine_id, 0))
                for row in rows
            ]
            if active_only:
                machines = [machine for machine in machines if machine["status"] == "online"]
            return self._sort_public_machines(machines)

    def get_machine(self, machine_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.get(RemoteCodexMachineModel, machine_id)
            if row is None:
                return None
            thread_count = db.scalar(
                select(func.count(RemoteCodexThreadModel.id)).where(RemoteCodexThreadModel.machine_id == machine_id)
            ) or 0
            return self._machine_row_to_public(row, thread_count=thread_count)

    def get_threads(self, machine_id: str, *, query: str = "", limit: int = 60) -> list[dict[str, Any]] | None:
        with session_scope() as db:
            machine = db.get(RemoteCodexMachineModel, machine_id)
            if machine is None:
                return None
            rows = list(
                db.scalars(
                    select(RemoteCodexThreadModel)
                    .where(RemoteCodexThreadModel.machine_id == machine_id)
                    .order_by(RemoteCodexThreadModel.updated_at_ms.desc(), RemoteCodexThreadModel.title.asc())
                )
            )
            items = [self._thread_row_to_public(row) for row in rows]
            normalized_query = compact_text(query).lower()
            if normalized_query:
                items = [
                    item
                    for item in items
                    if any(
                        normalized_query in compact_text(item.get(field)).lower()
                        for field in ("title", "cwd", "firstUserMessage")
                    )
                ]
            return items[: max(1, min(limit, 200))]

    def get_thread(self, machine_id: str, thread_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.scalar(
                select(RemoteCodexThreadModel).where(
                    RemoteCodexThreadModel.machine_id == machine_id,
                    RemoteCodexThreadModel.thread_id == thread_id,
                )
            )
            return self._thread_row_to_public(row) if row is not None else None

    def get_thread_snapshot(
        self,
        machine_id: str,
        thread_id: str,
        *,
        limit: int = DEFAULT_THREAD_MESSAGE_LIMIT,
        after_line_number: int = 0,
    ) -> dict[str, Any] | None:
        with session_scope() as db:
            machine_row = db.get(RemoteCodexMachineModel, machine_id)
            if machine_row is None:
                return None
            thread_row = db.scalar(
                select(RemoteCodexThreadModel).where(
                    RemoteCodexThreadModel.machine_id == machine_id,
                    RemoteCodexThreadModel.thread_id == thread_id,
                )
            )
            if thread_row is None:
                return None
            stale_command_ids, deletable_command_ids = self._stale_thread_command_ids(db, machine_id, thread_id)
            if deletable_command_ids:
                db.execute(
                    delete(RemoteCodexCommandModel).where(
                        RemoteCodexCommandModel.command_id.in_(deletable_command_ids)
                    )
                )
            message_rows = list(
                db.scalars(
                    select(RemoteCodexMessageModel)
                    .where(
                        RemoteCodexMessageModel.thread_row_id == thread_row.id,
                        RemoteCodexMessageModel.line_number > max(0, int(after_line_number)),
                    )
                    .order_by(RemoteCodexMessageModel.line_number.asc())
                )
            )
            public_messages = [self._message_row_to_public(row) for row in message_rows]
            recent_user_texts = {
                normalize_message_text(message.get("text"))
                for message in public_messages
                if compact_text(message.get("role")) == "user"
            }
            command_rows = list(
                db.scalars(
                    select(RemoteCodexCommandModel)
                    .where(
                        RemoteCodexCommandModel.machine_id == machine_id,
                        RemoteCodexCommandModel.thread_id == thread_id,
                    )
                    .order_by(RemoteCodexCommandModel.created_at.asc())
                )
            )
            if stale_command_ids:
                command_rows = [row for row in command_rows if row.command_id not in stale_command_ids]
            task_ids = sorted({row.task_id for row in command_rows if row.task_id})
            task_status_by_id: dict[str, str] = {}
            if task_ids:
                task_status_by_id = {
                    row.id: compact_text(row.status).lower()
                    for row in db.scalars(select(RemoteTaskModel).where(RemoteTaskModel.id.in_(task_ids)))
                }
            pending_command_messages = [
                message
                for message in (
                    self._command_row_to_pending_message(
                        row,
                        recent_user_texts=recent_user_texts,
                        task_status_by_id=task_status_by_id,
                    )
                    for row in command_rows
                )
                if message is not None
            ]
            public_messages.extend(pending_command_messages)
            effective_limit = max(0, int(limit))
            if effective_limit > 0:
                effective_limit = min(effective_limit, DEFAULT_STORED_MESSAGE_WINDOW)
            if effective_limit > 0:
                public_messages = public_messages[-effective_limit:]
            return {
                "machine": self._machine_row_to_public(
                    machine_row,
                    thread_count=db.scalar(
                        select(func.count(RemoteCodexThreadModel.id)).where(
                            RemoteCodexThreadModel.machine_id == machine_id
                        )
                    )
                    or 0,
                ),
                "thread": self._thread_row_to_public(thread_row),
                "messages": public_messages,
                "totalMessages": thread_row.total_messages + len(pending_command_messages),
                "lineCount": thread_row.line_count,
                "fileSize": thread_row.file_size,
                "syncedAt": isoformat(thread_row.synced_at),
            }

    def apply_agent_sync(
        self,
        *,
        machine: dict[str, Any],
        threads: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        changed_thread_ids: set[str] = set()
        machine_id = compact_text(machine.get("machineId") or machine.get("machine_id"))
        if not machine_id:
            raise ValueError("machine.machineId is required.")

        with session_scope() as db:
            machine_row = db.get(RemoteCodexMachineModel, machine_id)
            if machine_row is None:
                machine_row = RemoteCodexMachineModel(machine_id=machine_id)
                db.add(machine_row)

            machine_row.display_name = compact_text(machine.get("displayName") or machine.get("display_name"), machine_id)
            machine_row.source = compact_text(machine.get("source"), "agent")
            machine_row.active_transport = compact_text(
                machine.get("activeTransport") or machine.get("active_transport"),
                "filesystem-storage",
            )
            machine_row.runtime_mode = compact_text(
                machine.get("runtimeMode") or machine.get("runtime_mode"),
                "filesystem-readonly",
            )
            machine_row.runtime_available = bool(machine.get("runtimeAvailable") or machine.get("runtime_available"))
            machine_row.capabilities_json = dumps_json(machine.get("capabilities") or {})
            machine_row.runtime_descriptor_json = serialize_maybe_json(
                machine.get("runtimeDescriptor") or machine.get("runtime_descriptor")
            )
            machine_row.last_runtime_error = machine.get("lastRuntimeError") or machine.get("last_runtime_error")
            machine_row.last_diagnostic = machine.get("lastDiagnostic") or machine.get("last_diagnostic")
            machine_row.last_seen_at = ensure_utc(datetime.fromisoformat(machine.get("lastSeenAt"))) if machine.get("lastSeenAt") else utcnow()
            machine_row.last_sync_at = ensure_utc(datetime.fromisoformat(machine.get("lastSyncAt"))) if machine.get("lastSyncAt") else utcnow()

            existing_threads = {
                row.thread_id: row
                for row in db.scalars(
                    select(RemoteCodexThreadModel).where(RemoteCodexThreadModel.machine_id == machine_id)
                )
            }
            incoming_ids = {
                compact_text(item.get("id"))
                for item in threads
                if compact_text(item.get("id"))
            }
            for thread_id, stale_row in existing_threads.items():
                if thread_id not in incoming_ids:
                    db.delete(stale_row)

            for item in threads:
                thread_id = compact_text(item.get("id"))
                if not thread_id:
                    continue
                row = existing_threads.get(thread_id)
                if row is None:
                    row = RemoteCodexThreadModel(machine_id=machine_id, thread_id=thread_id)
                    db.add(row)
                    existing_threads[thread_id] = row
                row.title = compact_text(item.get("title"), "(untitled)")
                row.cwd = compact_text(item.get("cwd"))
                row.rollout_path = compact_text(item.get("rolloutPath") or item.get("rollout_path"))
                row.updated_at_ms = coerce_int(item.get("updatedAtMs"))
                row.created_at_ms = coerce_int(item.get("createdAtMs"))
                row.source = compact_text(item.get("source")) or None
                row.model_provider = compact_text(item.get("modelProvider") or item.get("model_provider")) or None
                row.model = compact_text(item.get("model")) or None
                row.reasoning_effort = compact_text(item.get("reasoningEffort") or item.get("reasoning_effort")) or None
                row.cli_version = compact_text(item.get("cliVersion") or item.get("cli_version")) or None
                row.first_user_message = compact_text(item.get("firstUserMessage") or item.get("first_user_message"))
                row.forked_from_id = compact_text(item.get("forkedFromId") or item.get("forked_from_id")) or None
                row.ephemeral = bool(item.get("ephemeral"))
                row.status_json = serialize_maybe_json(item.get("status"))
                row.agent_nickname = compact_text(item.get("agentNickname") or item.get("agent_nickname")) or None
                row.agent_role = compact_text(item.get("agentRole") or item.get("agent_role")) or None
                changed_thread_ids.add(thread_id)

            for item in snapshots:
                thread_payload = item.get("thread") or {}
                thread_id = compact_text(thread_payload.get("id"))
                if not thread_id:
                    continue
                row = existing_threads.get(thread_id)
                if row is None:
                    row = RemoteCodexThreadModel(machine_id=machine_id, thread_id=thread_id)
                    db.add(row)
                    existing_threads[thread_id] = row
                row.title = compact_text(thread_payload.get("title"), row.title or "(untitled)")
                row.cwd = compact_text(thread_payload.get("cwd"), row.cwd or "")
                row.rollout_path = compact_text(thread_payload.get("rolloutPath") or thread_payload.get("rollout_path"), row.rollout_path or "")
                row.updated_at_ms = coerce_int(thread_payload.get("updatedAtMs"), row.updated_at_ms)
                row.created_at_ms = coerce_int(thread_payload.get("createdAtMs"), row.created_at_ms)
                row.source = compact_text(thread_payload.get("source"), row.source or "") or None
                row.model_provider = compact_text(thread_payload.get("modelProvider") or thread_payload.get("model_provider"), row.model_provider or "") or None
                row.model = compact_text(thread_payload.get("model"), row.model or "") or None
                row.reasoning_effort = compact_text(thread_payload.get("reasoningEffort") or thread_payload.get("reasoning_effort"), row.reasoning_effort or "") or None
                row.cli_version = compact_text(thread_payload.get("cliVersion") or thread_payload.get("cli_version"), row.cli_version or "") or None
                row.first_user_message = compact_text(thread_payload.get("firstUserMessage") or thread_payload.get("first_user_message"), row.first_user_message or "")
                row.forked_from_id = compact_text(thread_payload.get("forkedFromId") or thread_payload.get("forked_from_id"), row.forked_from_id or "") or None
                row.ephemeral = bool(thread_payload.get("ephemeral", row.ephemeral))
                row.status_json = serialize_maybe_json(thread_payload.get("status")) or row.status_json
                row.agent_nickname = compact_text(thread_payload.get("agentNickname") or thread_payload.get("agent_nickname"), row.agent_nickname or "") or None
                row.agent_role = compact_text(thread_payload.get("agentRole") or thread_payload.get("agent_role"), row.agent_role or "") or None
                row.total_messages = coerce_int(item.get("totalMessages"), row.total_messages)
                row.line_count = coerce_int(item.get("lineCount"), row.line_count)
                row.file_size = coerce_int(item.get("fileSize"), row.file_size)
                row.synced_at = ensure_utc(datetime.fromisoformat(item.get("syncedAt"))) if item.get("syncedAt") else utcnow()
                db.flush()
                db.execute(delete(RemoteCodexMessageModel).where(RemoteCodexMessageModel.thread_row_id == row.id))
                for message in list(item.get("messages") or [])[-DEFAULT_STORED_MESSAGE_WINDOW:]:
                    db.add(
                        RemoteCodexMessageModel(
                            thread_row_id=row.id,
                            line_number=coerce_int(message.get("lineNumber")),
                            timestamp=message.get("timestamp"),
                            role=compact_text(message.get("role"), "assistant"),
                            phase=compact_text(message.get("phase")) or None,
                            text=str(message.get("text") or ""),
                            images_json=dumps_json(message.get("images") or [], fallback="[]"),
                        )
                    )
                changed_thread_ids.add(thread_id)

            db.flush()
            for thread_id in sorted(existing_threads.keys()):
                _stale_ids, deletable_ids = self._stale_thread_command_ids(db, machine_id, thread_id)
                if deletable_ids:
                    db.execute(
                        delete(RemoteCodexCommandModel).where(
                            RemoteCodexCommandModel.command_id.in_(deletable_ids)
                        )
                    )
            machine_public = self._machine_row_to_public(
                machine_row,
                thread_count=db.scalar(
                    select(func.count(RemoteCodexThreadModel.id)).where(
                        RemoteCodexThreadModel.machine_id == machine_id
                    )
                )
                or 0,
            )

        self._publish(machine_id, "*", {"kind": "machine", "machine": machine_public})
        for thread_id in changed_thread_ids:
            self._publish(machine_id, thread_id, {"kind": "snapshot"})
        return machine_public

    def list_thread_commands(
        self,
        machine_id: str,
        thread_id: str,
        *,
        limit: int = 8,
        include_stale: bool = False,
    ) -> list[dict[str, Any]]:
        with session_scope() as db:
            stale_command_ids, deletable_command_ids = self._stale_thread_command_ids(db, machine_id, thread_id)
            if deletable_command_ids:
                db.execute(
                    delete(RemoteCodexCommandModel).where(
                        RemoteCodexCommandModel.command_id.in_(deletable_command_ids)
                    )
                )
            rows = list(
                db.scalars(
                    select(RemoteCodexCommandModel)
                    .where(
                        RemoteCodexCommandModel.machine_id == machine_id,
                        RemoteCodexCommandModel.thread_id == thread_id,
                    )
                    .order_by(RemoteCodexCommandModel.updated_at.desc(), RemoteCodexCommandModel.created_at.desc())
                    .limit(max(1, min(limit, 30)))
                )
            )
            if not include_stale and stale_command_ids:
                rows = [row for row in rows if row.command_id not in stale_command_ids]
            return [self._command_row_to_public(row) for row in rows]

    def get_active_thread_command(self, machine_id: str, thread_id: str, *, command_type: str | None = None) -> dict[str, Any] | None:
        commands = self.list_thread_commands(machine_id, thread_id, limit=20)
        for command in commands:
            if command_type and command["type"] != command_type:
                continue
            if command["status"] in {COMMAND_QUEUED, COMMAND_RUNNING}:
                return command
        return None

    def get_latest_turn_id(self, machine_id: str, thread_id: str) -> str | None:
        commands = self.list_thread_commands(machine_id, thread_id, limit=20)
        for command in commands:
            if command["type"] == TURN_START and command["status"] == COMMAND_COMPLETED and command["turnId"]:
                return command["turnId"]
        return None

    def enqueue_command(
        self,
        *,
        command_type: str,
        machine_id: str,
        thread_id: str,
        requested_by: dict[str, Any],
        prompt: str | None = None,
        turn_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        with session_scope() as db:
            row = RemoteCodexCommandModel(
                type=command_type,
                status=COMMAND_QUEUED,
                machine_id=machine_id,
                thread_id=thread_id,
                task_id=task_id,
                turn_id=turn_id,
                prompt=prompt,
                requested_by_json=serialize_requested_by(requested_by),
            )
            db.add(row)
            db.flush()
            public = self._command_row_to_public(row)
            self._mirror_command_enqueue(db, row=row, command_type=command_type)
        self._publish(machine_id, thread_id, {"kind": "command", "command": public})
        return public

    def claim_next_command(self, machine_id: str, *, worker_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.scalar(
                select(RemoteCodexCommandModel)
                .where(
                    RemoteCodexCommandModel.machine_id == machine_id,
                    RemoteCodexCommandModel.status == COMMAND_QUEUED,
                )
                .order_by(RemoteCodexCommandModel.created_at.asc())
                .limit(1)
            )
            if row is None:
                return None
            row.status = COMMAND_RUNNING
            row.worker_id = compact_text(worker_id, "unknown-worker")
            row.started_at = utcnow()
            row.updated_at = row.started_at
            db.flush()
            public = self._command_row_to_public(row, include_attachment_data=True)
            broadcast = self._command_row_to_public(row)
            self._mirror_command_transition(
                db,
                command_id=row.command_id,
                status=TASK_STATUS_CLAIMED,
                owner_actor_id=row.worker_id,
            )
        self._publish(machine_id, public["threadId"], {"kind": "command", "command": broadcast})
        return public

    def complete_command(self, command_id: str, *, worker_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(RemoteCodexCommandModel, command_id)
            if row is None:
                raise ValueError(f"Unknown command: {command_id}")
            row.status = COMMAND_COMPLETED
            row.worker_id = compact_text(worker_id, row.worker_id or "")
            row.result_json = dumps_json(result or {}, fallback="{}")
            row.completed_at = utcnow()
            row.updated_at = row.completed_at
            if isinstance(result, dict):
                next_turn_id = compact_text(result.get("turnId"))
                if next_turn_id:
                    row.turn_id = next_turn_id
            db.flush()
            public = self._command_row_to_public(row)
            self._mirror_command_transition(
                db,
                command_id=row.command_id,
                status=TASK_STATUS_COMPLETED,
                owner_actor_id=row.worker_id,
                result=result,
            )
        self._publish(public["machineId"], public["threadId"], {"kind": "command", "command": public})
        return public

    def fail_command(self, command_id: str, *, worker_id: str, error: dict[str, Any] | None) -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(RemoteCodexCommandModel, command_id)
            if row is None:
                raise ValueError(f"Unknown command: {command_id}")
            row.status = COMMAND_FAILED
            row.worker_id = compact_text(worker_id, row.worker_id or "")
            row.error_json = dumps_json(error or {"message": "Unknown error"})
            row.completed_at = utcnow()
            row.updated_at = row.completed_at
            db.flush()
            public = self._command_row_to_public(row)
            self._mirror_command_transition(
                db,
                command_id=row.command_id,
                status=TASK_STATUS_FAILED,
                owner_actor_id=row.worker_id,
                error=error,
            )
        self._publish(public["machineId"], public["threadId"], {"kind": "command", "command": public})
        return public
