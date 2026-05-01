"""AgentRuntime -- inbox loop scaffold for an autonomous agent.

The runtime walks the actor's inbox, dispatches each unread event to
a user-supplied handler, advances ``last_seen_seq`` after the handler
returns, then sleeps. The handler decides what to send back via the
client (speech / evidence / close / nothing).

Designed to be swappable for a streaming SSE backend later -- the
``IncomingEvent`` shape is the contract.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from .client import BridgeV2Client


@dataclass(frozen=True)
class IncomingEvent:
    operation_id: str
    operation_kind: str
    operation_state: str
    operation_title: str
    role: str
    seq: int
    kind: str
    actor_id: str
    payload: dict
    addressed_to_actor_ids: list[str]
    private_to_actor_ids: list[str] | None
    replies_to_event_id: str | None


class AgentHandler(Protocol):
    def __call__(self, event: IncomingEvent, client: BridgeV2Client) -> None: ...


class AgentRuntime:
    def __init__(
        self,
        client: BridgeV2Client,
        handler: AgentHandler,
        *,
        poll_interval_seconds: float = 2.0,
        kinds_filter: list[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._handler = handler
        self._interval = poll_interval_seconds
        self._kinds = kinds_filter
        self._sleep = sleep
        self._running = False

    def stop(self) -> None:
        self._running = False

    def run_forever(self) -> None:
        self._running = True
        while self._running:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 -- keep loop alive on transient errors
                pass
            self._sleep(self._interval)

    def run_once(self) -> int:
        """Process one polling tick. Returns number of events
        dispatched -- useful for tests that drive the runtime
        synchronously."""
        inbox = self._client.get_inbox(state="open")
        dispatched = 0
        for item in inbox.get("items", []):
            op_id = item["operation_id"]
            participants = self._client.get_operation(op_id).get("participants", [])
            mine = next(
                (p for p in participants if p.get("role") == item["role"]),
                None,
            )
            after_seq = (mine or {}).get("last_seen_seq") or 0
            events_body = self._client.list_events(
                op_id, after_seq=after_seq, kinds=self._kinds,
            )
            events = events_body.get("events", [])
            if not events:
                continue
            for ev in events:
                incoming = IncomingEvent(
                    operation_id=op_id,
                    operation_kind=item["kind"],
                    operation_state=item["state"],
                    operation_title=item["title"],
                    role=item["role"],
                    seq=ev["seq"],
                    kind=ev["kind"],
                    actor_id=ev["actor_id"],
                    payload=ev.get("payload") or {},
                    addressed_to_actor_ids=ev.get("addressed_to_actor_ids") or [],
                    private_to_actor_ids=ev.get("private_to_actor_ids"),
                    replies_to_event_id=ev.get("replies_to_event_id"),
                )
                self._handler(incoming, self._client)
                dispatched += 1
            # Advance cursor to the highest seq we dispatched.
            highest = events[-1]["seq"]
            self._client.mark_seen(op_id, seq=highest)
        return dispatched
