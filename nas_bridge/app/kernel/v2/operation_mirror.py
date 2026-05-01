"""OperationMirror -- writes a v2 Operation alongside v1 chat conversation
lifecycle within the SAME db session.

The mirror is intentionally one-way: v1 stays authoritative through F7,
and v2 catches up via these calls. Once F7 flips the read path and
F8 retires v1, the mirror inverts and the v1 writes get dropped.

The v1 row hands its id back via ``v2_operation_id`` so the close path
can locate the mirror without scanning ``metadata_json``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .actor_service import ActorService
from .repository import V2Repository


def _normalize_handle(actor: str | None) -> str | None:
    if not actor:
        return None
    return actor if actor.startswith("@") else f"@{actor}"


class OperationMirror:
    def __init__(
        self,
        repo: V2Repository | None = None,
        actor_service: ActorService | None = None,
    ) -> None:
        self._repo = repo or V2Repository()
        self._actors = actor_service or ActorService(self._repo)

    def mirror_conversation_open(
        self,
        db: Session,
        *,
        v1_conversation_id: str,
        thread_id: str,
        kind: str,
        title: str,
        intent: str | None,
        opener_actor: str,
        owner_actor: str | None,
        addressed_to: str | None,
        is_general: bool,
    ) -> str:
        """Returns v2 operation id."""
        opener_handle = _normalize_handle(opener_actor) or "@system"
        opener = self._actors.ensure_actor_by_handle(
            db, handle=opener_handle, kind="human" if opener_handle == "@system" else "ai",
        )
        op = self._repo.insert_operation(
            db,
            space_id=f"chat:{thread_id}",
            kind=kind,
            title=title,
            intent=intent,
            metadata={
                "v1_conversation_id": v1_conversation_id,
                "is_general": is_general,
            },
        )
        self._repo.add_participant(
            db, operation_id=op.id, actor_id=opener.id, role="opener",
        )
        owner_handle = _normalize_handle(owner_actor)
        if owner_handle and owner_handle != opener_handle:
            owner = self._actors.ensure_actor_by_handle(db, handle=owner_handle)
            self._repo.add_participant(
                db, operation_id=op.id, actor_id=owner.id, role="owner",
            )
        addr_handle = _normalize_handle(addressed_to)
        if addr_handle and addr_handle not in {opener_handle, owner_handle}:
            addr = self._actors.ensure_actor_by_handle(db, handle=addr_handle)
            self._repo.add_participant(
                db, operation_id=op.id, actor_id=addr.id, role="addressed",
            )
        return op.id

    def mirror_message(
        self,
        db: Session,
        *,
        v2_operation_id: str | None,
        actor_name: str,
        event_kind: str,
        content: str,
        addressed_to: str | None,
        addressed_to_many: list[str] | None,
        replies_to_v2_event_id: str | None,
        private_to_actors: list[str] | None = None,
    ) -> str | None:
        """Mirror a v1 ChatMessage row into a v2 OperationEvent. Returns
        the v2 event id (or None if the conversation has no v2 mirror)."""
        if not v2_operation_id:
            return None
        actor = self._actors.ensure_actor_by_handle(
            db, handle=_normalize_handle(actor_name) or "@system",
            kind="human" if actor_name == "system" else "ai",
        )
        addressed_handles = []
        primary = _normalize_handle(addressed_to)
        if primary:
            addressed_handles.append(primary)
        for extra in addressed_to_many or []:
            extra_h = _normalize_handle(extra)
            if extra_h and extra_h not in addressed_handles:
                addressed_handles.append(extra_h)
        addressed_actor_ids: list[str] = []
        for handle in addressed_handles:
            row = self._actors.ensure_actor_by_handle(db, handle=handle)
            addressed_actor_ids.append(row.id)
        private_actor_ids: list[str] | None = None
        if private_to_actors:
            private_actor_ids = []
            for handle in private_to_actors:
                handle_norm = _normalize_handle(handle)
                if not handle_norm:
                    continue
                row = self._actors.ensure_actor_by_handle(db, handle=handle_norm)
                private_actor_ids.append(row.id)
        ev = self._repo.insert_event(
            db,
            operation_id=v2_operation_id,
            actor_id=actor.id,
            kind=event_kind,
            payload={"text": content} if event_kind.startswith("chat.speech.") else {"content": content},
            addressed_to_actor_ids=addressed_actor_ids or None,
            replies_to_event_id=replies_to_v2_event_id,
            private_to_actor_ids=private_actor_ids,
        )
        return ev.id

    def mirror_conversation_close(
        self,
        db: Session,
        *,
        v2_operation_id: str | None,
        closed_by_actor: str | None,
        resolution: str,
        resolution_summary: str | None,
    ) -> None:
        if not v2_operation_id:
            return
        closer_id: str | None = None
        closer_handle = _normalize_handle(closed_by_actor)
        if closer_handle and closer_handle != "@system":
            closer = self._actors.ensure_actor_by_handle(db, handle=closer_handle)
            closer_id = closer.id
        self._repo.close_operation(
            db,
            operation_id=v2_operation_id,
            closed_by_actor_id=closer_id,
            resolution=resolution,
            resolution_summary=resolution_summary,
            closed_at=datetime.now(timezone.utc),
        )
