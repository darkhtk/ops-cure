"""Actor service -- the identity boundary for protocol v2.

Bridges the existing auth shape (single shared token + self-asserted
``X-Bridge-Client-Id``) to the v2 Actor model. The asserted client_id
becomes the actor's handle. First sighting auto-provisions an Actor
row; subsequent calls reuse it.

When per-agent tokens land later, ``ActorTokenBindingV2`` table can
join here without changing callers.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .repository import V2Repository

DEFAULT_OPERATOR_HANDLE = "@operator"


class ActorService:
    def __init__(self, repo: V2Repository | None = None) -> None:
        self._repo = repo or V2Repository()

    def ensure_actor_by_handle(
        self,
        db: Session,
        *,
        handle: str,
        display_name: str | None = None,
        kind: str = "ai",
        capabilities: list[str] | None = None,
    ):
        existing = self._repo.get_actor_by_handle(db, handle)
        if existing is not None:
            self._repo.update_actor_presence(
                db,
                actor_id=existing.id,
                status="online",
                last_seen_at=datetime.now(timezone.utc),
            )
            return existing
        return self._repo.insert_actor(
            db,
            handle=handle,
            display_name=display_name or handle,
            kind=kind,
            capabilities=capabilities or [],
            status="online",
        )

    def actor_for_caller(
        self,
        db: Session,
        *,
        asserted_client_id: str | None,
        operator_handle: str = DEFAULT_OPERATOR_HANDLE,
    ):
        """Translate a bridge caller into a v2 Actor row.

        If the caller asserted a client_id, that becomes the handle (kind
        defaults to ``ai`` -- it is some downstream automation). If no
        client_id is asserted, the request is treated as the human
        operator behind the shared token.
        """
        if asserted_client_id:
            return self.ensure_actor_by_handle(
                db,
                handle=f"@{asserted_client_id}",
                display_name=asserted_client_id,
                kind="ai",
            )
        return self.ensure_actor_by_handle(
            db,
            handle=operator_handle,
            display_name="Operator",
            kind="human",
        )
