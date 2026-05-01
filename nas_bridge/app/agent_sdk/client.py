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

    # ---- write side: native v2 endpoints (G2) ----
    # The SDK speaks v2 exclusively. The bridge dual-writes to v1 under
    # the hood through F8; clients never see the v1 conversation id.

    def open_operation(
        self,
        *,
        space_id: str,
        kind: str,
        title: str,
        intent: str | None = None,
        addressed_to: str | None = None,
        objective: str | None = None,
        success_criteria: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "space_id": space_id,
            "kind": kind,
            "title": title,
            "opener_actor_handle": self._actor_handle,
        }
        if intent is not None:
            body["intent"] = intent
        if addressed_to is not None:
            body["addressed_to"] = addressed_to
        if objective is not None:
            body["objective"] = objective
        if success_criteria is not None:
            body["success_criteria"] = success_criteria
        return self._post("/v2/operations", json=body)

    def append_event(
        self,
        operation_id: str,
        *,
        kind: str,
        text: str,
        addressed_to: str | None = None,
        addressed_to_many: list[str] | None = None,
        replies_to_event_id: str | None = None,
        private_to_actors: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "actor_handle": self._actor_handle,
            "kind": kind,
            "payload": {"text": text},
        }
        if addressed_to is not None:
            body["addressed_to"] = addressed_to
        if addressed_to_many:
            body["addressed_to_many"] = addressed_to_many
        if replies_to_event_id is not None:
            body["replies_to_event_id"] = replies_to_event_id
        if private_to_actors:
            body["private_to_actors"] = private_to_actors
        return self._post(
            f"/v2/operations/{operation_id}/events",
            json=body,
        )

    def close_operation(
        self,
        operation_id: str,
        *,
        resolution: str,
        summary: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "actor_handle": self._actor_handle,
            "resolution": resolution,
        }
        if summary is not None:
            body["summary"] = summary
        return self._post(
            f"/v2/operations/{operation_id}/close",
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
