from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class ChatParticipantStateStore(Protocol):
    def get_cursor(self, *, actor_name: str, thread_id: str) -> str | None: ...

    def set_cursor(self, *, actor_name: str, thread_id: str, message_id: str) -> None: ...

    def get_event_cursor(self, *, actor_name: str, thread_id: str) -> str | None: ...

    def set_event_cursor(self, *, actor_name: str, thread_id: str, event_cursor: str) -> None: ...


def _cursor_key(*, actor_name: str, thread_id: str) -> str:
    return f"{actor_name}::{thread_id}"


def _coerce_entry(raw_entry: object) -> dict[str, str]:
    if isinstance(raw_entry, str):
        return {"message_id": raw_entry}
    if not isinstance(raw_entry, dict):
        return {}
    return {str(key): str(value) for key, value in raw_entry.items()}


class InMemoryChatParticipantStateStore:
    def __init__(self) -> None:
        self._entries: dict[str, dict[str, str]] = {}

    def get_cursor(self, *, actor_name: str, thread_id: str) -> str | None:
        return self._entries.get(_cursor_key(actor_name=actor_name, thread_id=thread_id), {}).get("message_id")

    def set_cursor(self, *, actor_name: str, thread_id: str, message_id: str) -> None:
        entry = self._entries.setdefault(_cursor_key(actor_name=actor_name, thread_id=thread_id), {})
        entry["message_id"] = message_id

    def get_event_cursor(self, *, actor_name: str, thread_id: str) -> str | None:
        return self._entries.get(_cursor_key(actor_name=actor_name, thread_id=thread_id), {}).get("event_cursor")

    def set_event_cursor(self, *, actor_name: str, thread_id: str, event_cursor: str) -> None:
        entry = self._entries.setdefault(_cursor_key(actor_name=actor_name, thread_id=thread_id), {})
        entry["event_cursor"] = event_cursor


class JsonFileChatParticipantStateStore:
    def __init__(self, *, path: str | Path) -> None:
        self.path = Path(path)

    def get_cursor(self, *, actor_name: str, thread_id: str) -> str | None:
        entry = self._load_entry(actor_name=actor_name, thread_id=thread_id)
        return entry.get("message_id")

    def set_cursor(self, *, actor_name: str, thread_id: str, message_id: str) -> None:
        payload = self._load()
        key = _cursor_key(actor_name=actor_name, thread_id=thread_id)
        entry = _coerce_entry(payload.get(key))
        entry["message_id"] = message_id
        payload[key] = entry
        self._save(payload)

    def get_event_cursor(self, *, actor_name: str, thread_id: str) -> str | None:
        entry = self._load_entry(actor_name=actor_name, thread_id=thread_id)
        return entry.get("event_cursor")

    def set_event_cursor(self, *, actor_name: str, thread_id: str, event_cursor: str) -> None:
        payload = self._load()
        key = _cursor_key(actor_name=actor_name, thread_id=thread_id)
        entry = _coerce_entry(payload.get(key))
        entry["event_cursor"] = event_cursor
        payload[key] = entry
        self._save(payload)

    def _load_entry(self, *, actor_name: str, thread_id: str) -> dict[str, str]:
        payload = self._load()
        raw_entry = payload.get(_cursor_key(actor_name=actor_name, thread_id=thread_id))
        return _coerce_entry(raw_entry)

    def _load(self) -> dict[str, dict[str, str] | str]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        payload: dict[str, dict[str, str] | str] = {}
        for key, value in raw.items():
            if isinstance(value, str):
                payload[str(key)] = value
                continue
            if isinstance(value, dict):
                payload[str(key)] = {str(item_key): str(item_value) for item_key, item_value in value.items()}
        return payload

    def _save(self, payload: dict[str, dict[str, str] | str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self.path)
