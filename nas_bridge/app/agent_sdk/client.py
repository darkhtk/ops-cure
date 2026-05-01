"""HTTP client wrapping protocol v2 routes."""
from __future__ import annotations

from typing import Any

import httpx


class BridgeV2Error(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"bridge {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class BridgeV2Client:
    """Synchronous client. Async variant can be layered on top of httpx
    AsyncClient when an agent needs concurrency; the v2 API is small
    enough that the sync surface covers F11."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        actor_handle: str,
        timeout: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._actor_handle = (
            actor_handle if actor_handle.startswith("@") else f"@{actor_handle}"
        )
        client_id = self._actor_handle.lstrip("@")
        self._http = httpx.Client(
            base_url=self._base,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "X-Bridge-Client-Id": client_id,
            },
        )

    @property
    def actor_handle(self) -> str:
        return self._actor_handle

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BridgeV2Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- inbox ----

    def get_inbox(
        self,
        *,
        state: str | None = None,
        roles: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"actor_handle": self._actor_handle, "limit": limit}
        if state:
            params["state"] = state
        if roles:
            params["roles"] = ",".join(roles)
        return self._get("/v2/inbox", params=params)

    def get_unread_count(self) -> int:
        body = self._get(
            "/v2/inbox/unread-count",
            params={"actor_handle": self._actor_handle},
        )
        return int(body.get("unread_total", 0))

    # ---- operation reads ----

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self._get(f"/v2/operations/{operation_id}")

    def list_events(
        self,
        operation_id: str,
        *,
        after_seq: int | None = None,
        kinds: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "actor_handle": self._actor_handle,
            "limit": limit,
        }
        if after_seq is not None:
            params["after_seq"] = after_seq
        if kinds:
            params["kinds"] = ",".join(kinds)
        return self._get(f"/v2/operations/{operation_id}/events", params=params)

    def list_artifacts(
        self,
        operation_id: str,
        *,
        kind: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if kind:
            params["kind"] = kind
        return self._get(
            f"/v2/operations/{operation_id}/artifacts",
            params=params,
        )

    def mark_seen(self, operation_id: str, seq: int) -> dict[str, Any]:
        return self._post(
            f"/v2/operations/{operation_id}/seen",
            params={"actor_handle": self._actor_handle, "seq": seq},
        )

    # ---- write side: speech / open / close use existing v1 endpoints ----
    # These hit the chat surface which is dual-written; v2 picks them
    # up automatically. Once F8's hard removal lands, swap to native v2
    # endpoints (POST /v2/operations/.../events).

    def open_conversation(
        self,
        *,
        discord_thread_id: str,
        kind: str,
        title: str,
        intent: str | None = None,
        addressed_to: str | None = None,
        objective: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": kind,
            "title": title,
            "opener_actor": self._actor_handle.lstrip("@"),
        }
        if intent is not None:
            body["intent"] = intent
        if addressed_to is not None:
            body["addressed_to"] = addressed_to
        if objective is not None:
            body["objective"] = objective
        return self._post(
            f"/api/chat/threads/{discord_thread_id}/conversations",
            json=body,
        )

    def submit_speech(
        self,
        *,
        conversation_id: str,
        kind: str,
        content: str,
        addressed_to: str | None = None,
        addressed_to_many: list[str] | None = None,
        replies_to_speech_id: str | None = None,
        private_to_actors: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "actor_name": self._actor_handle.lstrip("@"),
            "kind": kind,
            "content": content,
        }
        if addressed_to is not None:
            body["addressed_to"] = addressed_to
        if addressed_to_many:
            body["addressed_to_many"] = addressed_to_many
        if replies_to_speech_id is not None:
            body["replies_to_speech_id"] = replies_to_speech_id
        if private_to_actors:
            body["private_to_actors"] = private_to_actors
        return self._post(
            f"/api/chat/conversations/{conversation_id}/speech",
            json=body,
        )

    def close_conversation(
        self,
        *,
        conversation_id: str,
        resolution: str,
        summary: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "closed_by": self._actor_handle.lstrip("@"),
            "resolution": resolution,
        }
        if summary is not None:
            body["summary"] = summary
        return self._post(
            f"/api/chat/conversations/{conversation_id}/close",
            json=body,
        )

    # ---- transport ----

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self._http.get(path, params=params)
        return self._unwrap(r)

    def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        r = self._http.post(path, json=json, params=params)
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: httpx.Response) -> dict[str, Any]:
        if r.status_code >= 400:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text
            raise BridgeV2Error(r.status_code, detail)
        if not r.content:
            return {}
        return r.json()
