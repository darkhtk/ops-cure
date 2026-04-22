from __future__ import annotations

import asyncio
from collections import deque
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock

from .events import EventEnvelope, SubscriptionPresenceSummary


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class BrokerSubscription:
    broker: "InProcessSubscriptionBroker"
    subscription_id: str
    space_id: str
    kinds: frozenset[str] = field(default_factory=frozenset)
    subscriber_id: str | None = None
    queue: asyncio.Queue[EventEnvelope] = field(default_factory=asyncio.Queue)
    accepted_after_cursor: str | None = None
    latest_cursor: str | None = None
    reset_reason: str | None = None

    async def next_event(self, *, timeout_seconds: float | None = None) -> EventEnvelope | None:
        self.broker.touch(subscriber_id=self.subscriber_id, space_id=self.space_id)
        try:
            if timeout_seconds is None:
                item = await self.queue.get()
            else:
                item = await asyncio.wait_for(self.queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return None
        self.broker.touch(subscriber_id=self.subscriber_id, space_id=self.space_id)
        return item

    def close(self) -> None:
        self.broker.unsubscribe(subscription_id=self.subscription_id)


class InProcessSubscriptionBroker:
    def __init__(self, *, presence_ttl_seconds: int = 60, backlog_limit: int = 1024) -> None:
        self.presence_ttl_seconds = presence_ttl_seconds
        self.backlog_limit = backlog_limit
        self._lock = RLock()
        self._subscriptions: dict[str, dict[str, BrokerSubscription]] = {}
        self._backlog: dict[str, deque[EventEnvelope]] = {}
        self._presence: dict[tuple[str, str], SubscriptionPresenceSummary] = {}

    def publish(self, *, space_id: str, item: EventEnvelope) -> None:
        with self._lock:
            backlog = self._backlog.setdefault(space_id, deque(maxlen=self.backlog_limit))
            backlog.append(item)
            listeners = tuple(self._subscriptions.get(space_id, {}).values())
        for subscription in listeners:
            if subscription.kinds and item.event.kind not in subscription.kinds:
                continue
            subscription.queue.put_nowait(item)
            self.touch(subscriber_id=subscription.subscriber_id, space_id=space_id)

    def subscribe(
        self,
        *,
        space_id: str,
        after_cursor: str | None = None,
        kinds: list[str] | None = None,
        subscriber_id: str | None = None,
    ) -> BrokerSubscription:
        subscription = BrokerSubscription(
            broker=self,
            subscription_id=str(uuid.uuid4()),
            space_id=space_id,
            kinds=frozenset(kinds or []),
            subscriber_id=subscriber_id,
            accepted_after_cursor=after_cursor,
        )
        with self._lock:
            self._subscriptions.setdefault(space_id, {})[subscription.subscription_id] = subscription
            backlog = list(self._backlog.get(space_id, ()))
            subscription.latest_cursor = backlog[-1].cursor if backlog else None
            if after_cursor and backlog:
                oldest_cursor = backlog[0].cursor
                if after_cursor < oldest_cursor:
                    subscription.reset_reason = "cursor_out_of_window"
                else:
                    replay_items = [
                        item
                        for item in backlog
                        if item.cursor > after_cursor
                        and (not subscription.kinds or item.event.kind in subscription.kinds)
                    ]
                    for item in replay_items:
                        subscription.queue.put_nowait(item)
        self.touch(subscriber_id=subscriber_id, space_id=space_id)
        return subscription

    def unsubscribe(self, *, subscription_id: str) -> None:
        with self._lock:
            for space_id, subscriptions in list(self._subscriptions.items()):
                if subscription_id in subscriptions:
                    del subscriptions[subscription_id]
                    if not subscriptions:
                        del self._subscriptions[space_id]
                    return

    def touch(self, *, subscriber_id: str | None, space_id: str) -> None:
        if not subscriber_id:
            return
        now = utcnow()
        self._presence[(space_id, subscriber_id)] = SubscriptionPresenceSummary(
            subscriber_id=subscriber_id,
            space_id=space_id,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=self.presence_ttl_seconds),
        )

    def list_presence(self, *, space_id: str) -> list[SubscriptionPresenceSummary]:
        now = utcnow()
        expired = [
            key
            for key, summary in self._presence.items()
            if summary.expires_at is not None and summary.expires_at <= now
        ]
        for key in expired:
            self._presence.pop(key, None)
        return [
            summary
            for (summary_space_id, _), summary in sorted(self._presence.items())
            if summary_space_id == space_id
        ]
