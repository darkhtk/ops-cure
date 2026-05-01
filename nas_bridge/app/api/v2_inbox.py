"""Inbox API on protocol v2.

GET /v2/inbox?actor_handle=@bob[&state=open][&roles=opener,owner]

Returns every Operation actor `@bob` participates in, ordered most
recently active first. Drives the "what needs my attention" view that
v1 didn't have a single endpoint for. Reads exclusively from
operation_participants_v2 + operations_v2.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import BridgeCaller, require_bridge_caller
from ..db import session_scope
from ..kernel.v2 import V2Repository

router = APIRouter(prefix="/v2", tags=["v2-inbox"])


def _serialize_operation(op, role: str, repo: V2Repository) -> dict[str, Any]:
    return {
        "operation_id": op.id,
        "space_id": op.space_id,
        "kind": op.kind,
        "state": op.state,
        "title": op.title,
        "intent": op.intent,
        "role": role,
        "resolution": op.resolution,
        "opened_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
        "closed_at": op.closed_at.isoformat() if op.closed_at else None,
    }


@router.get("/inbox")
def get_inbox(
    actor_handle: str = Query(..., description="Actor handle, e.g. '@bob'"),
    state: str | None = Query(default=None, description="Filter by op state (open/closed)"),
    roles: str | None = Query(default=None, description="Comma-separated roles to include"),
    limit: int = Query(default=100, ge=1, le=500),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    role_filter: list[str] | None = None
    if roles:
        role_filter = [r.strip() for r in roles.split(",") if r.strip()]
    handle = actor_handle if actor_handle.startswith("@") else f"@{actor_handle}"
    repo = V2Repository()
    items: list[dict[str, Any]] = []
    with session_scope() as db:
        actor = repo.get_actor_by_handle(db, handle)
        if actor is None:
            # No row yet -> empty inbox; not a 404 because the answer
            # ("nothing waiting on you") is correct either way.
            return {"actor_handle": handle, "items": []}
        pairs = repo.operations_for_actor(
            db,
            actor_id=actor.id,
            roles=role_filter,
            state=state,
            limit=limit,
        )
        for op, role in pairs:
            items.append(_serialize_operation(op, role, repo))
    return {"actor_handle": handle, "items": items}


@router.get("/inbox/unread-count")
def get_unread_count(
    actor_handle: str = Query(...),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """Total unread events across the actor's participated operations.

    Computed from ``operation_participants_v2.last_seen_seq`` vs the
    operation's current MAX(seq). Quick path -- useful for badge
    counts in agent loops without paginating the full inbox.
    """
    handle = actor_handle if actor_handle.startswith("@") else f"@{actor_handle}"
    repo = V2Repository()
    with session_scope() as db:
        actor = repo.get_actor_by_handle(db, handle)
        if actor is None:
            return {"actor_handle": handle, "unread_total": 0}
        pairs = repo.operations_for_actor(db, actor_id=actor.id)
        unread_total = 0
        for op, _role in pairs:
            participants = repo.list_participants(db, operation_id=op.id)
            mine = next(
                (p for p in participants if p.actor_id == actor.id),
                None,
            )
            last_seen = mine.last_seen_seq if (mine and mine.last_seen_seq is not None) else 0
            events = repo.list_events(db, operation_id=op.id, after_seq=last_seen, limit=500)
            # only events the actor is addressed-to or on a public op
            for ev in events:
                addressed = repo.event_addressed_to(ev)
                private_to = repo.event_private_to(ev)
                if private_to is not None and actor.id not in private_to:
                    continue
                if addressed and actor.id not in addressed:
                    # not directly addressed -- still count as "in operation"
                    pass
                unread_total += 1
    return {"actor_handle": handle, "unread_total": unread_total}
