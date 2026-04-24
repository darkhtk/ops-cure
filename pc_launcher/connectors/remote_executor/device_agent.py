from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..chat_participant.runtime import AppServerThreadClient
from .bridge import RemoteExecutorBridge


LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_THREADS = 60
DEFAULT_MESSAGE_LIMIT = 200
DEFAULT_TURN_MESSAGE_LOOKBACK = 24
DEFAULT_RECENT_SIGNATURE_WINDOW = 80
REMOTE_CODEX_PENDING_ENTRY_FLAG = "remote_codex_pending"
REMOTE_CODEX_PENDING_COMMAND_ID_KEY = "commandId"
REMOTE_CODEX_PENDING_PROMPT_TTL_SECONDS = 6 * 60 * 60


def compact_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    normalized = " ".join(text.split()).strip()
    return normalized or fallback


def normalize_windows_path(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.removeprefix("\\\\?\\")


def normalize_epoch_ms(value: Any) -> int:
    try:
        numeric = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if numeric <= 0:
        return 0
    return numeric if numeric >= 1_000_000_000_000 else numeric * 1000


def compact_preview(value: Any, *, max_length: int = 280) -> str:
    text = compact_text(value)
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def default_machine_id() -> str:
    return compact_text(socket.gethostname().lower(), "local-machine")


def default_machine_display_name() -> str:
    return compact_text(socket.gethostname(), "Local Codex")


def normalize_thread_status(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    text = compact_text(value)
    return {"type": text} if text else None


def normalize_runtime_thread(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    thread_id = compact_text(item.get("id"))
    if not thread_id:
        return None
    preview = compact_text(item.get("preview") or item.get("firstUserMessage"))
    title = compact_text(item.get("name") or item.get("title"), preview or "(untitled)")
    return {
        "id": thread_id,
        "title": title,
        "cwd": compact_text(item.get("cwd")),
        "rolloutPath": normalize_windows_path(item.get("path") or item.get("rolloutPath") or item.get("rollout_path")),
        "updatedAtMs": normalize_epoch_ms(item.get("updatedAt") or item.get("updatedAtMs")),
        "createdAtMs": normalize_epoch_ms(item.get("createdAt") or item.get("createdAtMs")),
        "source": compact_text(item.get("source")) or None,
        "modelProvider": compact_text(item.get("modelProvider") or item.get("model_provider")) or None,
        "model": compact_text(item.get("model")) or None,
        "reasoningEffort": compact_text(item.get("reasoningEffort") or item.get("reasoning_effort")) or None,
        "cliVersion": compact_text(item.get("cliVersion") or item.get("cli_version")) or None,
        "firstUserMessage": preview,
        "forkedFromId": compact_text(item.get("forkedFromId") or item.get("forked_from_id")) or None,
        "ephemeral": bool(item.get("ephemeral")),
        "status": normalize_thread_status(item.get("status")),
        "agentNickname": compact_text(item.get("agentNickname") or item.get("agent_nickname")) or None,
        "agentRole": compact_text(item.get("agentRole") or item.get("agent_role")) or None,
    }


def normalize_runtime_thread_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        if isinstance(result.get("data"), list):
            items = result["data"]
        elif isinstance(result.get("threads"), list):
            items = result["threads"]
        else:
            items = []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    normalized: list[dict[str, Any]] = []
    for item in items:
        thread = normalize_runtime_thread(item)
        if thread is not None:
            normalized.append(thread)
    return normalized


def merge_thread_snapshots(primary: dict[str, Any] | None, fallback: dict[str, Any] | None) -> dict[str, Any] | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    merged = dict(fallback)
    merged.update(primary)
    for key in (
        "title",
        "cwd",
        "rolloutPath",
        "source",
        "modelProvider",
        "model",
        "reasoningEffort",
        "cliVersion",
        "firstUserMessage",
        "forkedFromId",
        "agentNickname",
        "agentRole",
    ):
        if not compact_text(merged.get(key)):
            merged[key] = fallback.get(key)
    if not merged.get("updatedAtMs"):
        merged["updatedAtMs"] = fallback.get("updatedAtMs", 0)
    if not merged.get("createdAtMs"):
        merged["createdAtMs"] = fallback.get("createdAtMs", 0)
    if merged.get("status") is None:
        merged["status"] = fallback.get("status")
    return merged


def build_thread_version(thread: dict[str, Any]) -> str:
    status = thread.get("status")
    status_text = (
        compact_text(status.get("type")) if isinstance(status, dict) else compact_text(status)
    )
    rollout_path = Path(normalize_windows_path(thread.get("rolloutPath"))).expanduser()
    rollout_stat = ""
    if rollout_path.exists():
        stat_result = rollout_path.stat()
        rollout_stat = f"{stat_result.st_mtime_ns}:{stat_result.st_size}"
    return ":".join(
        [
            str(int(thread.get("updatedAtMs") or 0)),
            status_text,
            compact_text(thread.get("rolloutPath")),
            rollout_stat,
            compact_text(thread.get("cliVersion")),
            compact_text(thread.get("title")),
        ]
    )


def _extract_message_image(item: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    image_url = ""
    for key in ("image_url", "url", "src"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            image_url = candidate.strip()
            break
    if not image_url:
        return None

    alt = compact_text(
        item.get("alt")
        or item.get("title")
        or item.get("label")
        or item.get("name"),
        f"Uploaded image {index}",
    )
    title = compact_text(item.get("title") or item.get("label") or item.get("name")) or None
    return {
        "src": image_url,
        "alt": alt,
        "title": title,
    }


def extract_message_parts(content: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(content, list):
        return "", []

    text_parts: list[str] = []
    images: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue

        item_type = compact_text(item.get("type")).lower()
        if item_type in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
            continue

        if item_type in {"input_image", "output_image", "image"}:
            image = _extract_message_image(item, index=len(images) + 1)
            if image is not None:
                images.append(image)

    if images:
        text_parts = [part for part in text_parts if compact_text(part) != "<image>"]

    return "\n\n".join(part.strip() for part in text_parts if part.strip()).strip(), images


def should_include_message(
    role: str | None,
    text: str,
    *,
    images: list[dict[str, Any]] | None = None,
) -> bool:
    if role not in {"user", "assistant"}:
        return False
    normalized = text.strip()
    if not normalized:
        return bool(images)
    hidden_prefixes = (
        "<environment_context>",
        "<permissions instructions>",
        "<app-context>",
        "<collaboration_mode>",
        "<skills_instructions>",
    )
    return not any(normalized.startswith(prefix) for prefix in hidden_prefixes)


def _extract_turn_item_message_content(item: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text = ""
    images: list[dict[str, Any]] = []

    content = item.get("content")
    if isinstance(content, list):
        text, images = extract_message_parts(content)

    if not text:
        text = compact_text(item.get("text"))

    return text, images


def normalize_turn_item_message(
    item: dict[str, Any],
    *,
    sequence_number: int,
    phase: str | None = None,
) -> dict[str, Any] | None:
    item_type = compact_text(item.get("type"))
    if item_type == "userMessage":
        role = "user"
    elif item_type == "agentMessage":
        role = "assistant"
    else:
        return None

    text, images = _extract_turn_item_message_content(item)
    normalized_phase = compact_text(item.get("phase")) or phase or None
    if not should_include_message(role, text, images=images):
        return None
    return {
        "lineNumber": sequence_number,
        "timestamp": item.get("timestamp"),
        "role": role,
        "phase": normalized_phase if role == "assistant" else None,
        "text": text,
        "images": images,
    }


def message_signature(message: dict[str, Any]) -> tuple[Any, ...]:
    images = message.get("images") if isinstance(message.get("images"), list) else []
    image_sources = tuple(
        compact_text(item.get("src"))
        for item in images
        if isinstance(item, dict) and compact_text(item.get("src"))
    )
    return (
        compact_text(message.get("role")),
        compact_text(message.get("phase")),
        compact_text(message.get("text")),
        image_sources,
    )


def merge_missing_turn_messages(
    rollout_messages: list[dict[str, Any]],
    turn_messages: list[dict[str, Any]],
    *,
    recent_window: int = DEFAULT_RECENT_SIGNATURE_WINDOW,
) -> list[dict[str, Any]]:
    if not turn_messages:
        return list(rollout_messages)

    merged_messages = list(rollout_messages)
    recent_known = {
        message_signature(message)
        for message in merged_messages[-max(1, recent_window) :]
    }
    pending_messages: list[dict[str, Any]] = []

    for message in turn_messages:
        signature = message_signature(message)
        if signature in recent_known:
            continue
        previous = pending_messages[-1] if pending_messages else None
        if not merge_adjacent_message(previous, message):
            pending_messages.append(message)
        recent_known.add(signature)

    if pending_messages:
        merged_messages.extend(pending_messages)
    return merged_messages


def build_recent_turn_messages(
    thread_payload: dict[str, Any] | None,
    *,
    lookback_turns: int = DEFAULT_TURN_MESSAGE_LOOKBACK,
) -> list[dict[str, Any]]:
    if not isinstance(thread_payload, dict):
        return []
    thread = thread_payload.get("thread") if isinstance(thread_payload.get("thread"), dict) else {}
    turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
    if not turns:
        return []

    recent_turns = turns[-max(1, lookback_turns) :]
    messages: list[dict[str, Any]] = []
    sequence_number = 1_000_000
    for turn in recent_turns:
        if not isinstance(turn, dict):
            continue
        turn_phase = compact_text(turn.get("status")) or None
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            message = normalize_turn_item_message(
                item,
                sequence_number=sequence_number,
                phase=turn_phase,
            )
            sequence_number += 1
            if message is None:
                continue
            previous = messages[-1] if messages else None
            if not merge_adjacent_message(previous, message):
                messages.append(message)
    return messages


def normalize_rollout_message(entry: dict[str, Any], *, line_number: int) -> dict[str, Any] | None:
    entry_type = compact_text(entry.get("type"))
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    role: str | None = None
    phase: str | None = None
    text = ""
    images: list[dict[str, Any]] = []
    if entry_type == "response_item" and compact_text(payload.get("type")) == "message":
        role = compact_text(payload.get("role"))
        phase = compact_text(payload.get("phase")) or None
        text, images = extract_message_parts(payload.get("content"))
    elif entry_type == "event_msg":
        payload_type = compact_text(payload.get("type"))
        if payload_type == "user_message":
            role = "user"
            text = compact_text(payload.get("message") or payload.get("text"))
        elif payload_type == "agent_message":
            role = "assistant"
            phase = compact_text(payload.get("phase")) or None
            text = compact_text(payload.get("message") or payload.get("text"))
    if not should_include_message(role, text, images=images):
        return None
    return {
        "lineNumber": line_number,
        "timestamp": entry.get("timestamp"),
        "role": role,
        "phase": phase,
        "text": text,
        "images": images,
    }


def _rollout_entry_text(entry: dict[str, Any]) -> str:
    message = normalize_rollout_message(entry, line_number=0)
    if message is None:
        return ""
    return compact_text(message.get("text"))


def _is_pending_remote_codex_entry(entry: dict[str, Any]) -> bool:
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    return bool(payload.get(REMOTE_CODEX_PENDING_ENTRY_FLAG))


def _reconcile_pending_rollout_entries(lines: list[str]) -> tuple[list[str], bool]:
    now = datetime.now(timezone.utc)
    pending_by_text: dict[str, list[int]] = {}
    drop_indices: set[int] = set()
    parsed_entries: list[dict[str, Any] | None] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            parsed_entries.append(None)
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            parsed_entries.append(None)
            continue
        parsed_entries.append(entry)

        text = _rollout_entry_text(entry)
        if not text:
            continue

        if _is_pending_remote_codex_entry(entry):
            timestamp_text = compact_text(entry.get("timestamp"))
            if timestamp_text:
                try:
                    created_at = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
                except ValueError:
                    created_at = None
            else:
                created_at = None
            if created_at is not None:
                age_seconds = max(0.0, (now - created_at).total_seconds())
                if age_seconds >= REMOTE_CODEX_PENDING_PROMPT_TTL_SECONDS:
                    drop_indices.add(index)
                    continue
            pending_by_text.setdefault(text, []).append(index)
            continue

        message = normalize_rollout_message(entry, line_number=0)
        if message is None or compact_text(message.get("role")) != "user":
            continue
        pending_indices = pending_by_text.get(text)
        if pending_indices:
            drop_indices.add(pending_indices.pop(0))

    if not drop_indices:
        return lines, False
    reconciled = [line for index, line in enumerate(lines) if index not in drop_indices]
    return reconciled, True


def merge_adjacent_message(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if previous is None:
        return False
    if previous.get("role") != current.get("role"):
        return False
    if previous.get("phase") != current.get("phase"):
        return False
    if compact_text(previous.get("text")) != compact_text(current.get("text")):
        return False
    try:
        previous_line = int(previous.get("lineNumber") or 0)
        current_line = int(current.get("lineNumber") or 0)
    except (TypeError, ValueError):
        return False
    if abs(previous_line - current_line) > 2:
        return False
    previous_images = previous.get("images") if isinstance(previous.get("images"), list) else []
    current_images = current.get("images") if isinstance(current.get("images"), list) else []
    if current_images:
        seen_sources = {
            compact_text(item.get("src"))
            for item in previous_images
            if isinstance(item, dict) and compact_text(item.get("src"))
        }
        for item in current_images:
            if not isinstance(item, dict):
                continue
            source = compact_text(item.get("src"))
            if not source or source in seen_sources:
                continue
            previous_images.append(item)
            seen_sources.add(source)
        previous["images"] = previous_images
    previous["lineNumber"] = max(previous_line, current_line)
    previous["timestamp"] = current.get("timestamp") or previous.get("timestamp")
    return True


def extract_turn_id(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return None
    turn = result.get("turn")
    if isinstance(turn, dict):
        turn_id = compact_text(turn.get("id"))
        if turn_id:
            return turn_id
    return compact_text(result.get("id")) or None


def extract_turn_status(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return None
    turn = result.get("turn")
    if isinstance(turn, dict):
        status = compact_text(turn.get("status"))
        if status:
            return status
    return compact_text(result.get("status")) or None


class LocalCodexBackend:
    def __init__(
        self,
        *,
        machine_id: str,
        display_name: str,
        app_server_client: AppServerThreadClient | None = None,
        codex_home: str | None = None,
        runtime_descriptor: dict[str, Any] | None = None,
    ) -> None:
        self.machine_id = machine_id
        self.display_name = display_name
        self.app_server_client = app_server_client
        self.codex_home = Path(codex_home or os.getenv("CODEX_HOME") or (Path.home() / ".codex")).resolve()
        self.state_db_path = self.codex_home / "state_5.sqlite"
        self.runtime_descriptor = runtime_descriptor or {}
        self.last_runtime_error: str | None = None
        self.runtime_available = False
        self.active_transport = "filesystem-storage"

    def _archived_thread_ids(self) -> set[str]:
        if not self.state_db_path.exists():
            return set()
        with sqlite3.connect(self.state_db_path) as connection:
            rows = connection.execute("select id from threads where archived = 1").fetchall()
        return {compact_text(row[0]) for row in rows if compact_text(row[0])}

    def _is_archived(self, thread_id: str) -> bool:
        if not thread_id or not self.state_db_path.exists():
            return False
        with sqlite3.connect(self.state_db_path) as connection:
            row = connection.execute("select archived from threads where id = ? limit 1", (thread_id,)).fetchone()
        return bool(row and int(row[0] or 0) == 1)

    def _query_threads(self, query: str = "", *, limit: int = DEFAULT_MAX_THREADS) -> list[dict[str, Any]]:
        if not self.state_db_path.exists():
            return []
        safe_limit = max(1, min(int(limit), 200))
        filters = ["archived = 0"]
        params: list[Any] = []
        normalized_query = compact_text(query)
        if normalized_query:
            like = f"%{normalized_query}%"
            filters.append("(title like ? or cwd like ? or first_user_message like ?)")
            params.extend([like, like, like])
        sql = (
            "select "
            "id, title, cwd, rollout_path as rolloutPath, updated_at_ms as updatedAtMs, "
            "created_at_ms as createdAtMs, source, model_provider as modelProvider, model, "
            "reasoning_effort as reasoningEffort, cli_version as cliVersion, "
            "first_user_message as firstUserMessage "
            "from threads "
            f"where {' and '.join(filters)} "
            "order by updated_at_ms desc "
            f"limit {safe_limit}"
        )
        with sqlite3.connect(self.state_db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "title": compact_text(row["title"], "(untitled)"),
                "cwd": compact_text(row["cwd"]),
                "rolloutPath": normalize_windows_path(row["rolloutPath"]),
                "updatedAtMs": normalize_epoch_ms(row["updatedAtMs"]),
                "createdAtMs": normalize_epoch_ms(row["createdAtMs"]),
                "source": compact_text(row["source"]) or None,
                "modelProvider": compact_text(row["modelProvider"]) or None,
                "model": compact_text(row["model"]) or None,
                "reasoningEffort": compact_text(row["reasoningEffort"]) or None,
                "cliVersion": compact_text(row["cliVersion"]) or None,
                "firstUserMessage": compact_text(row["firstUserMessage"]),
                "forkedFromId": None,
                "ephemeral": False,
                "status": None,
                "agentNickname": None,
                "agentRole": None,
            }
            for row in rows
        ]

    def _get_stored_thread(self, thread_id: str) -> dict[str, Any] | None:
        if not self.state_db_path.exists():
            return None
        sql = (
            "select "
            "id, title, cwd, rollout_path as rolloutPath, updated_at_ms as updatedAtMs, "
            "created_at_ms as createdAtMs, source, model_provider as modelProvider, model, "
            "reasoning_effort as reasoningEffort, cli_version as cliVersion, "
            "first_user_message as firstUserMessage "
            "from threads where id = ? limit 1"
        )
        with sqlite3.connect(self.state_db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(sql, (thread_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "title": compact_text(row["title"], "(untitled)"),
            "cwd": compact_text(row["cwd"]),
            "rolloutPath": normalize_windows_path(row["rolloutPath"]),
            "updatedAtMs": normalize_epoch_ms(row["updatedAtMs"]),
            "createdAtMs": normalize_epoch_ms(row["createdAtMs"]),
            "source": compact_text(row["source"]) or None,
            "modelProvider": compact_text(row["modelProvider"]) or None,
            "model": compact_text(row["model"]) or None,
            "reasoningEffort": compact_text(row["reasoningEffort"]) or None,
            "cliVersion": compact_text(row["cliVersion"]) or None,
            "firstUserMessage": compact_text(row["firstUserMessage"]),
            "forkedFromId": None,
            "ephemeral": False,
            "status": None,
            "agentNickname": None,
            "agentRole": None,
        }

    def _resolve_rollout_path_for_thread(self, thread_id: str) -> Path | None:
        thread = self.get_thread_by_id(thread_id)
        if thread is None:
            return None
        rollout_path = Path(normalize_windows_path(thread.get("rolloutPath"))).expanduser()
        if not str(rollout_path):
            return None
        return rollout_path

    def materialize_pending_browser_prompt(
        self,
        *,
        thread_id: str,
        command_id: str,
        prompt: str,
    ) -> bool:
        rollout_path = self._resolve_rollout_path_for_thread(thread_id)
        if rollout_path is None:
            return False
        rollout_path.parent.mkdir(parents=True, exist_ok=True)

        existing_lines: list[str] = []
        if rollout_path.exists():
            existing_lines = rollout_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in existing_lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not _is_pending_remote_codex_entry(entry):
                    continue
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                if compact_text(payload.get(REMOTE_CODEX_PENDING_COMMAND_ID_KEY)) == compact_text(command_id):
                    return False

        entry = {
            "type": "event_msg",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "type": "user_message",
                "message": prompt,
                REMOTE_CODEX_PENDING_ENTRY_FLAG: True,
                REMOTE_CODEX_PENDING_COMMAND_ID_KEY: command_id,
                "threadId": thread_id,
            },
        }
        with rollout_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False))
            handle.write("\n")
        return True

    def _runtime_list_threads(self, *, limit: int) -> list[dict[str, Any]]:
        if self.app_server_client is None:
            return []
        try:
            result = self.app_server_client.list_threads(limit=limit)
            threads = normalize_runtime_thread_list(result)
            self.runtime_available = True
            self.active_transport = "standalone-app-server"
            self.last_runtime_error = None
            return threads
        except Exception as error:  # pragma: no cover - depends on local runtime
            self.runtime_available = False
            self.active_transport = "filesystem-storage"
            self.last_runtime_error = compact_text(getattr(error, "message", None) or str(error))
            LOGGER.warning("Remote executor could not list threads via app-server: %s", self.last_runtime_error)
            return []

    def _runtime_read_thread(self, thread_id: str) -> dict[str, Any] | None:
        if self.app_server_client is None:
            return None
        try:
            result = self.app_server_client.read_thread(thread_id)
        except Exception as error:  # pragma: no cover - depends on local runtime
            self.runtime_available = False
            self.active_transport = "filesystem-storage"
            self.last_runtime_error = compact_text(getattr(error, "message", None) or str(error))
            LOGGER.warning("Remote executor could not read thread %s via app-server: %s", thread_id, self.last_runtime_error)
            return None
        thread = result.get("thread") if isinstance(result, dict) else None
        return normalize_runtime_thread(thread or result)

    def get_health(self) -> dict[str, Any]:
        capabilities = {
            "threadRead": True,
            "threadLive": True,
            "liveControl": bool(self.runtime_available and self.app_server_client is not None),
            "approvalHandling": False,
        }
        return {
            "activeTransport": self.active_transport,
            "runtimeMode": "standalone-app-server" if self.app_server_client is not None else "filesystem-readonly",
            "runtimeAvailable": bool(self.runtime_available),
            "capabilities": capabilities,
            "runtimeDescriptor": self.runtime_descriptor or None,
            "lastRuntimeError": self.last_runtime_error,
            "lastDiagnostic": None,
        }

    def list_threads(self, *, limit: int = DEFAULT_MAX_THREADS, query: str = "") -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        runtime_threads = self._runtime_list_threads(limit=max(safe_limit, 200) if query else safe_limit)
        if runtime_threads:
            archived_ids = self._archived_thread_ids()
            if archived_ids:
                runtime_threads = [thread for thread in runtime_threads if compact_text(thread.get("id")) not in archived_ids]
            normalized_query = compact_text(query).lower()
            if normalized_query:
                runtime_threads = [
                    thread
                    for thread in runtime_threads
                    if any(
                        normalized_query in compact_text(thread.get(field)).lower()
                        for field in ("title", "cwd", "firstUserMessage")
                    )
                ]
            return runtime_threads[:safe_limit]
        return self._query_threads(query=query, limit=safe_limit)

    def get_thread_by_id(self, thread_id: str) -> dict[str, Any] | None:
        if self._is_archived(thread_id):
            return None
        return merge_thread_snapshots(self._runtime_read_thread(thread_id), self._get_stored_thread(thread_id))

    def read_thread_messages(self, thread_id: str, *, limit: int = DEFAULT_MESSAGE_LIMIT) -> dict[str, Any] | None:
        thread = self.get_thread_by_id(thread_id)
        if thread is None:
            return None
        live_thread_payload: dict[str, Any] | None = None
        if self.app_server_client is not None:
            try:
                live_thread_payload = self.app_server_client.read_thread(thread_id, include_turns=True)
                self.runtime_available = True
                self.active_transport = "standalone-app-server"
                self.last_runtime_error = None
            except Exception as error:  # pragma: no cover - depends on local runtime
                self.runtime_available = False
                self.last_runtime_error = compact_text(getattr(error, "message", None) or str(error))
                LOGGER.warning(
                    "Remote executor could not read thread %s turns via app-server: %s",
                    thread_id,
                    self.last_runtime_error,
                )
        rollout_path = Path(normalize_windows_path(thread.get("rolloutPath"))).expanduser()
        if not rollout_path.exists():
            messages = build_recent_turn_messages(live_thread_payload)
            return {
                "thread": thread,
                "messages": messages[-limit:] if limit > 0 else messages,
                "totalMessages": len(messages),
                "lineCount": 0,
                "fileSize": 0,
            }
        raw = rollout_path.read_text(encoding="utf-8", errors="replace")
        lines = raw.splitlines()
        reconciled_lines, changed = _reconcile_pending_rollout_entries(lines)
        if changed:
            rollout_path.write_text(
                "\n".join(reconciled_lines) + ("\n" if reconciled_lines else ""),
                encoding="utf-8",
            )
            lines = reconciled_lines
        messages: list[dict[str, Any]] = []
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            message = normalize_rollout_message(entry, line_number=index)
            if message is None:
                continue
            previous = messages[-1] if messages else None
            if not merge_adjacent_message(previous, message):
                messages.append(message)
        total_messages = len(messages)
        if limit > 0:
            messages = messages[-limit:]
        return {
            "thread": thread,
            "messages": messages,
            "totalMessages": total_messages,
            "lineCount": len(lines),
            "fileSize": rollout_path.stat().st_size,
        }

    def start_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        if self.app_server_client is None:
            raise RuntimeError("Local Codex live control is unavailable.")
        self.app_server_client.resume_thread(thread_id)
        result = self.app_server_client.start_turn(thread_id, prompt)
        self.runtime_available = True
        self.active_transport = "standalone-app-server"
        return result

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        if self.app_server_client is None:
            raise RuntimeError("Local Codex live control is unavailable.")
        result = self.app_server_client.interrupt_turn(thread_id, turn_id)
        self.runtime_available = True
        self.active_transport = "standalone-app-server"
        return result

    def delete_thread(self, thread_id: str) -> dict[str, Any]:
        if not self.state_db_path.exists():
            raise RuntimeError("Local Codex thread storage is unavailable.")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with sqlite3.connect(self.state_db_path) as connection:
            cursor = connection.execute(
                "update threads set archived = 1, updated_at_ms = ? where id = ? and archived = 0",
                (now_ms, thread_id),
            )
            connection.commit()
        if cursor.rowcount <= 0:
            raise RuntimeError("thread_not_found")
        return {"threadId": thread_id, "archived": True}


@dataclass(slots=True)
class RemoteCodexDeviceAgent:
    bridge: RemoteExecutorBridge
    backend: LocalCodexBackend
    machine_id: str
    display_name: str
    worker_id: str
    max_threads: int = DEFAULT_MAX_THREADS
    message_limit: int = DEFAULT_MESSAGE_LIMIT
    thread_versions: dict[str, str] = field(default_factory=dict)
    has_bootstrapped: bool = False

    def _build_machine_payload(self) -> dict[str, Any]:
        health = self.backend.get_health()
        now = datetime.now(timezone.utc).isoformat()
        return {
            "machineId": self.machine_id,
            "displayName": self.display_name,
            "source": "agent",
            "activeTransport": health["activeTransport"],
            "runtimeMode": health["runtimeMode"],
            "runtimeAvailable": health["runtimeAvailable"],
            "capabilities": health["capabilities"],
            "runtimeDescriptor": health["runtimeDescriptor"],
            "lastRuntimeError": health["lastRuntimeError"],
            "lastDiagnostic": health["lastDiagnostic"],
            "lastSeenAt": now,
            "lastSyncAt": now,
        }

    def perform_sync(self, *, force: bool = False) -> bool:
        threads = self.backend.list_threads(limit=self.max_threads)
        snapshots: list[dict[str, Any]] = []
        next_versions: dict[str, str] = {}
        for thread in threads:
            thread_id = compact_text(thread.get("id"))
            if not thread_id:
                continue
            version = build_thread_version(thread)
            next_versions[thread_id] = version
            if not force and self.thread_versions.get(thread_id) == version:
                continue
            snapshot = self.backend.read_thread_messages(thread_id, limit=self.message_limit)
            if snapshot is None:
                continue
            detailed_thread = self.backend.get_thread_by_id(thread_id) or snapshot.get("thread") or thread
            messages = list(snapshot.get("messages") or [])
            if self.message_limit > 0:
                messages = messages[-self.message_limit :]
            snapshots.append(
                {
                    "thread": detailed_thread,
                    "messages": messages,
                    "totalMessages": int(snapshot.get("totalMessages") or 0),
                    "lineCount": int(snapshot.get("lineCount") or 0),
                    "fileSize": int(snapshot.get("fileSize") or 0),
                    "syncedAt": self._build_machine_payload()["lastSyncAt"],
                }
            )
        self.bridge.sync_remote_codex_agent(
            machine=self._build_machine_payload(),
            threads=threads,
            snapshots=snapshots,
        )
        self.thread_versions = next_versions
        self.has_bootstrapped = True
        return bool(snapshots or threads)

    def mark_thread_dirty(self, thread_id: str) -> None:
        self.thread_versions.pop(thread_id, None)

    def execute_next_command(self) -> bool:
        command = self.bridge.claim_next_remote_codex_command(
            machine_id=self.machine_id,
            worker_id=self.worker_id,
        )
        if not command:
            return False

        command_id = compact_text(command.get("commandId"))
        task_id = compact_text(command.get("taskId")) or None
        thread_id = compact_text(command.get("threadId"))
        command_type = compact_text(command.get("type"))
        try:
            if task_id:
                self.bridge.heartbeat_remote_codex_agent_task(
                    task_id=task_id,
                    actor_id=self.machine_id,
                    phase="running",
                    summary="Submitting the browser request to the local Codex runtime.",
                )

            if command_type == "turn.start":
                prompt = compact_text(command.get("prompt"))
                if not prompt:
                    raise RuntimeError("Turn prompt is empty.")
                response = self.backend.start_turn(thread_id, prompt)
                if command_id:
                    self.backend.materialize_pending_browser_prompt(
                        thread_id=thread_id,
                        command_id=command_id,
                        prompt=prompt,
                    )
                result = {
                    "accepted": True,
                    "turnId": extract_turn_id(response),
                    "turnStatus": extract_turn_status(response),
                }
                if task_id:
                    self.bridge.add_remote_codex_agent_task_evidence(
                        task_id=task_id,
                        actor_id=self.machine_id,
                        kind="command_execution",
                        summary="Submitted a live turn command to the local Codex runtime.",
                        payload={
                            "commandId": command_id,
                            "turnId": result["turnId"],
                            "turnStatus": result["turnStatus"],
                            "type": command_type,
                        },
                    )
                    self.bridge.heartbeat_remote_codex_agent_task(
                        task_id=task_id,
                        actor_id=self.machine_id,
                        phase="executing",
                        summary="The local Codex runtime accepted the request and is updating the thread.",
                        commands_run_count=1,
                    )
            elif command_type == "turn.interrupt":
                turn_id = compact_text(command.get("turnId"))
                if not turn_id:
                    raise RuntimeError("Interrupt command is missing turnId.")
                self.backend.interrupt_turn(thread_id, turn_id)
                result = {
                    "interrupted": True,
                    "turnId": turn_id,
                }
                if task_id:
                    self.bridge.add_remote_codex_agent_task_evidence(
                        task_id=task_id,
                        actor_id=self.machine_id,
                        kind="command_execution",
                        summary="Sent an interrupt command to the local Codex runtime.",
                        payload={
                            "commandId": command_id,
                            "turnId": turn_id,
                            "type": command_type,
                        },
                    )
                    self.bridge.heartbeat_remote_codex_agent_task(
                        task_id=task_id,
                        actor_id=self.machine_id,
                        phase="interrupted",
                        summary="Interrupt reached the local Codex runtime.",
                        commands_run_count=1,
                    )
                    self.bridge.complete_remote_codex_agent_task(
                        task_id=task_id,
                        actor_id=self.machine_id,
                        summary="Interrupt request completed.",
                    )
            elif command_type == "thread.delete":
                response = self.backend.delete_thread(thread_id)
                result = {
                    "threadId": thread_id,
                    "archived": bool(response.get("archived")),
                }
            else:
                raise RuntimeError(f"Unsupported command type: {command_type}")

            self.bridge.report_remote_codex_command_result(
                command_id=command_id,
                worker_id=self.worker_id,
                status="completed",
                result=result,
            )
            self.mark_thread_dirty(thread_id)
            self.perform_sync(force=True)
            return True
        except Exception as error:
            error_text = compact_text(getattr(error, "message", None) or str(error), "Unknown error")
            if task_id:
                try:
                    self.bridge.fail_remote_codex_agent_task(
                        task_id=task_id,
                        actor_id=self.machine_id,
                        error_text=error_text,
                    )
                except Exception:
                    LOGGER.exception("Failed to mark remote_codex task %s as failed", task_id)
            try:
                self.bridge.report_remote_codex_command_result(
                    command_id=command_id,
                    worker_id=self.worker_id,
                    status="failed",
                    error={"message": error_text},
                )
            finally:
                self.mark_thread_dirty(thread_id)
                try:
                    self.perform_sync(force=True)
                except Exception:
                    LOGGER.exception("Remote executor sync after command failure did not complete cleanly")
            raise

    def poll_once(self) -> bool:
        activity = False
        if not self.has_bootstrapped:
            self.perform_sync(force=True)
            activity = True
        processed_command = False
        while self.execute_next_command():
            processed_command = True
            activity = True
        if not processed_command:
            self.perform_sync(force=False)
        return activity or processed_command
