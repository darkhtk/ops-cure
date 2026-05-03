"""Inbox API on protocol v2.

GET /v2/inbox?actor_handle=@bob[&state=open][&roles=opener,owner]

Returns every Operation actor `@bob` participates in, ordered most
recently active first. Drives the "what needs my attention" view that
v1 didn't have a single endpoint for. Reads exclusively from
operation_participants_v2 + operations_v2.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..auth import BridgeCaller, require_bridge_caller, verify_actor_handle_claim
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


@router.get("/operations/discoverable")
def list_discoverable_operations(
    actor_handle: str = Query(..., alias="for", description="Actor handle (e.g. '@bob') asking 'what could I join?'"),
    space_id: str | None = Query(default=None, description="Optional scope to a single space"),
    limit: int = Query(default=100, ge=1, le=500),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """v3 phase 2.5 — list operations the asker is **not yet a
    participant of** but **could legitimately join** under each op's
    ``policy.join_policy``. Closes the discovery gap noted in the
    mid-collab join review.

    Returns ``open`` ops only (closed ops can't be joined).

    Inclusion rules:
      * ``join_policy=open`` → always discoverable.
      * ``join_policy=self_or_invite`` → always discoverable (the asker
        can self-join under default policy).
      * ``join_policy=invite_only`` → only discoverable if the asker
        already has any participant role (typically ``addressed`` from
        a prior speech.invite); a self-join attempt would be rejected
        by the policy engine, so don't surface it.
    """
    handle = actor_handle if actor_handle.startswith("@") else f"@{actor_handle}"
    repo = V2Repository()
    items: list[dict[str, Any]] = []
    from ..kernel.v2 import contract as _v2_contract
    with session_scope() as db:
        from ..kernel.v2.models import OperationV2Model, OperationParticipantV2Model
        from sqlalchemy import select as _select
        actor = repo.get_actor_by_handle(db, handle)
        actor_id = actor.id if actor is not None else None

        # Pre-compute the set of op ids the actor is already in --
        # we exclude those (already in inbox).
        already_in: set[str] = set()
        if actor_id is not None:
            already_in = set(db.scalars(
                _select(OperationParticipantV2Model.operation_id)
                .where(OperationParticipantV2Model.actor_id == actor_id)
            ))

        stmt = _select(OperationV2Model).where(OperationV2Model.state == "open")
        if space_id:
            stmt = stmt.where(OperationV2Model.space_id == space_id)
        stmt = stmt.order_by(OperationV2Model.created_at.desc()).limit(max(1, min(int(limit), 500)))
        for op in db.scalars(stmt):
            if op.id in already_in:
                continue
            policy = repo.operation_policy(op)
            jp = policy.get("join_policy")
            if jp == _v2_contract.JOIN_POLICY_INVITE_ONLY:
                # Only surface invite_only ops where the asker is
                # already invited (existing participant).
                if actor_id is None:
                    continue
                # actor isn't in already_in (filtered above) so they
                # can't have been invited; skip.
                continue
            items.append({
                "id": op.id,
                "space_id": op.space_id,
                "kind": op.kind,
                "title": op.title,
                "intent": op.intent,
                "policy": policy,
                "created_at": op.created_at.isoformat() if op.created_at else None,
            })
    return {"actor_handle": handle, "items": items}


@router.get("/inbox/stream")
async def stream_inbox(
    request: Request,
    actor_handle: str = Query(..., description="Actor handle, e.g. '@bob'"),
    heartbeat_seconds: float = Query(default=15.0, ge=1.0, le=120.0),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
    x_actor_token: str | None = Header(default=None),
):
    # v3 phase 3.x: SSE subscribe is the most attractive impersonation
    # surface (anyone could read another agent's inbox). Verify
    # token↔handle binding before opening the stream.
    verify_actor_handle_claim(
        request, claimed_handle=actor_handle, x_actor_token=x_actor_token,
    )
    """G3: server-sent events stream of every v2 OperationEvent that
    lands in this actor's inbox. Each event is delivered with privacy
    redaction already applied (whisper events the actor isn't in are
    never published to its space).

    Heartbeat events ('event: heartbeat\\ndata: {}\\n\\n') keep the
    connection alive across idle periods. Clients should ignore them
    or use them as keepalive timestamps.
    """
    handle = actor_handle if actor_handle.startswith("@") else f"@{actor_handle}"
    # External agents subscribe by handle without prior registration. The
    # subscribe IS the registration -- auto-provision the actor row so the
    # caller can immediately receive routed events. Kind defaults to "ai"
    # because the SSE consumer is automation; the human-operator handle
    # path goes through actor_for_caller on the speech-submit side.
    from ..kernel.v2.actor_service import ActorService
    repo = V2Repository()
    with session_scope() as db:
        actor = ActorService(repo).ensure_actor_by_handle(
            db, handle=handle, display_name=handle.lstrip("@"), kind="ai",
        )
        actor_id = actor.id

    services = request.app.state.services
    broker = services.subscription_broker
    space_id = f"v2:inbox:{actor_id}"
    subscription = broker.subscribe(
        space_id=space_id,
        subscriber_id=f"sse:{actor_id}",
    )

    async def gen():
        try:
            yield (
                f"event: open\ndata: {json.dumps({'space_id': space_id, 'actor_id': actor_id})}\n\n"
            )
            while True:
                envelope = await subscription.next_event(
                    timeout_seconds=heartbeat_seconds,
                )
                if envelope is None:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                wrapped = _try_json(envelope.event.content)
                if not isinstance(wrapped, dict):
                    wrapped = {"payload": wrapped}
                payload = {
                    "operation_id": wrapped.get("operation_id"),
                    "event_id": envelope.event.id,
                    "seq": wrapped.get("seq"),
                    "kind": envelope.event.kind,
                    "actor_id": envelope.event.actor_name,
                    "payload": wrapped.get("payload"),
                    "addressed_to_actor_ids": wrapped.get("addressed_to_actor_ids", []),
                    "private_to_actor_ids": wrapped.get("private_to_actor_ids"),
                    "replies_to_event_id": wrapped.get("replies_to_event_id"),
                    # v3-additive: pass through expected_response so external
                    # agents can do mechanical "should I respond?" without
                    # heuristics or BROADCAST flags.
                    "expected_response": wrapped.get("expected_response"),
                    "created_at": envelope.event.created_at.isoformat() if envelope.event.created_at else None,
                    "cursor": envelope.cursor,
                }
                yield f"event: v2.event\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            subscription.close()

    return StreamingResponse(gen(), media_type="text/event-stream")


def _try_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


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
