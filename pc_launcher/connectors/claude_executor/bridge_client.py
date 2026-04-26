"""Thin HTTP client for the ops-cure remote_claude bridge.

Wraps the agent-side endpoints:
  POST /api/remote-claude/agent/sync
  POST /api/remote-claude/agent/commands/claim
  POST /api/remote-claude/agent/commands/{id}/result
  POST /api/remote-claude/agent/events

All requests carry a bearer token. Network failures surface as
RuntimeError so callers can decide to retry.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class BridgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        machine_id: str,
        worker_id: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.machine_id = machine_id
        self.worker_id = worker_id
        self.timeout_seconds = timeout_seconds

    # -------- public methods ----------------------------------------------

    def sync(self, machine: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/api/remote-claude/agent/sync", {
            "machine": machine,
            "sessions": sessions,
        })

    def claim_next(self) -> dict[str, Any] | None:
        body = self._post("/api/remote-claude/agent/commands/claim", {
            "machineId": self.machine_id,
            "workerId": self.worker_id,
        })
        return (body or {}).get("command")

    def report_result(
        self,
        command_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._post(f"/api/remote-claude/agent/commands/{command_id}/result", {
            "workerId": self.worker_id,
            "status": status,
            "result": result,
            "error": error,
        })

    def publish_event(
        self,
        *,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        self._post("/api/remote-claude/agent/events", {
            "machineId": self.machine_id,
            "sessionId": session_id,
            "event": event,
        })

    # -------- internals ---------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read()
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise RuntimeError(f"HTTP {e.code} from bridge {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"network error to bridge {path}: {e.reason}") from e
