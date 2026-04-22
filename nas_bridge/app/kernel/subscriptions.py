from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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
    def __init__(self, *, presence_ttl_seconds: int = 60) -> None:
        self.presence_ttl_seconds = presence_ttl_seconds
        self._subscriptions: dict[str, dict[str, BrokerSubscription]] = {}
        self._presence: dict[tuple[str, str], SubscriptionPresenceSummary] = {}

    def publish(self, *, space_id: str, item: EventEnvelope) -> None:
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
        del after_cursor
        subscription = BrokerSubscription(
            broker=self,
            subscription_id=str(uuid.uuid4()),
            space_id=space_id,
            kinds=frozenset(kinds or []),
            subscriber_id=subscriber_id,
        )
        self._subscriptions.setdefault(space_id, {})[subscription.subscription_id] = subscription
        self.touch(subscriber_id=subscriber_id, space_id=space_id)
        return subscription

    def unsubscribe(self, *, subscription_id: str) -> None:
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
