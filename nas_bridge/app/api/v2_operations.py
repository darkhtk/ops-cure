"""F7: v2 read endpoints — operation summary, event log, artifacts.

Reads come exclusively from operation_*_v2 tables. Privacy is enforced
here: events with ``private_to_actor_ids`` are filtered out unless
the requesting actor (taken from ``actor_handle`` query param) is in
the list. v1 routes remain in place but are now strictly authoritative
for fields v2 doesn't carry yet (e.g. v1-specific resolution vocab
strings round-trip exactly).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import BridgeCaller, require_bridge_caller
from ..db import session_scope
from ..kernel.v2 import V2Repository
from ..kernel.v2.models import OperationEventV2Model

router = APIRouter(prefix="/v2/operations", tags=["v2-operations"])


def _normalize_handle(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith("@") else f"@{value}"


def _serialize_event(ev, repo: V2Repository) -> dict[str, Any]:
    return {
        "id": ev.id,
        "operation_id": ev.operation_id,
        "actor_id": ev.actor_id,
        "seq": ev.seq,
        "kind": ev.kind,
        "payload": repo.event_payload(ev),
        "addressed_to_actor_ids": repo.event_addressed_to(ev),
        "private_to_actor_ids": repo.event_private_to(ev),
        "replies_to_event_id": ev.replies_to_event_id,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


def _serialize_operation(op, repo: V2Repository) -> dict[str, Any]:
    return {
        "id": op.id,
        "space_id": op.space_id,
        "kind": op.kind,
        "state": op.state,
        "title": op.title,
        "intent": op.intent,
        "metadata": repo.operation_metadata(op),
        "resolution": op.resolution,
        "resolution_summary": op.resolution_summary,
        "closed_by_actor_id": op.closed_by_actor_id,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
        "closed_at": op.closed_at.isoformat() if op.closed_at else None,
    }


def _resolve_actor_id(repo: V2Repository, db, actor_handle: str | None) -> str | None:
    handle = _normalize_handle(actor_handle)
    if not handle:
        return None
    actor = repo.get_actor_by_handle(db, handle)
    return actor.id if actor else None


@router.get("/{operation_id}")
def get_operation(
    operation_id: str,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        body = _serialize_operation(op, repo)
        body["participants"] = [
            {"actor_id": p.actor_id, "role": p.role, "last_seen_seq": p.last_seen_seq}
            for p in repo.list_participants(db, operation_id=op.id)
        ]
        return body


@router.get("/{operation_id}/events")
def list_events(
    operation_id: str,
    after_seq: int | None = Query(default=None, ge=0),
    kinds: str | None = Query(default=None, description="Comma-separated kinds"),
    actor_handle: str | None = Query(
        default=None,
        description="Requesting actor's handle for privacy filtering (whisper redaction).",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    kind_filter: list[str] | None = None
    if kinds:
        kind_filter = [k.strip() for k in kinds.split(",") if k.strip()]
    repo = V2Repository()
    items: list[dict[str, Any]] = []
    redacted_count = 0
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        viewer_id = _resolve_actor_id(repo, db, actor_handle)
        events = repo.list_events(
            db,
            operation_id=operation_id,
            after_seq=after_seq,
            kinds=kind_filter,
            limit=limit,
        )
        for ev in events:
            private_to = repo.event_private_to(ev)
            if private_to is not None:
                # whisper -- only the listed actors + the speaker may see
                if viewer_id is None or (viewer_id != ev.actor_id and viewer_id not in private_to):
                    redacted_count += 1
                    continue
            items.append(_serialize_event(ev, repo))
    return {
        "operation_id": operation_id,
        "events": items,
        "redacted_count": redacted_count,
        "viewer_actor_id": viewer_id,
    }


@router.get("/{operation_id}/artifacts")
def list_artifacts(
    operation_id: str,
    kind: str | None = Query(default=None),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        rows = repo.list_artifacts_for_operation(db, operation_id=operation_id, kind=kind)
        return {
            "operation_id": operation_id,
            "artifacts": [
                {
                    "id": a.id,
                    "event_id": a.event_id,
                    "kind": a.kind,
                    "uri": a.uri,
                    "sha256": a.sha256,
                    "mime": a.mime,
                    "size_bytes": a.size_bytes,
                    "label": a.label,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in rows
            ],
        }


@router.post("/{operation_id}/seen")
def mark_seen(
    operation_id: str,
    actor_handle: str = Query(...),
    seq: int = Query(..., ge=0),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """Advance the actor's last_seen_seq cursor on this operation.
    Idempotent and monotonic -- if seq is below the current cursor it
    is ignored. Drives unread badge counts."""
    repo = V2Repository()
    handle = _normalize_handle(actor_handle)
    with session_scope() as db:
        actor = repo.get_actor_by_handle(db, handle) if handle else None
        if actor is None:
            raise HTTPException(status_code=404, detail=f"actor {actor_handle!r} not found")
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        repo.update_participant_seen_seq(
            db, operation_id=operation_id, actor_id=actor.id, seq=seq,
        )
        return {"operation_id": operation_id, "actor_id": actor.id, "last_seen_seq": seq}


# G2: native v2 write endpoints. Internally these still delegate to
# ChatConversationService so v1 dual-write keeps producing v1 rows;
# clients no longer need to know about /api/chat or v1_conversation_id.
# Once F8's hard removal lands, the delegation collapses to v2-only
# writes.


def _strip_at(value: str) -> str:
    return value[1:] if value.startswith("@") else value


def _operation_to_v1_conversation_id(operation_id: str) -> str:
    """Look up the v1 conversation id linked to a v2 operation. Until
    F8 removes v1, we use this to route writes through the existing
    chat service."""
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        meta = repo.operation_metadata(op)
        v1_id = meta.get("v1_conversation_id")
        if not v1_id:
            raise HTTPException(
                status_code=409,
                detail=f"operation {operation_id} has no v1 mirror; cannot write",
            )
        return str(v1_id)


class V2OpenOperationRequest(BaseModel):
    """v2-flavored open. ``space_id`` carries the routing target; in the
    chat-only era this is the discord_thread_id (the bridge resolves
    it via chat_threads.discord_thread_id). When non-chat spaces land,
    ``space_kind`` becomes a discriminator."""
    space_id: str = Field(..., description="discord thread id (chat-only era)")
    kind: str
    title: str
    intent: str | None = None
    addressed_to: str | None = None
    opener_actor_handle: str
    objective: str | None = None
    success_criteria: dict[str, Any] | None = None


class V2EventRequest(BaseModel):
    actor_handle: str
    kind: str = Field(..., description="speech.claim / speech.question / ...")
    payload: dict[str, Any] = Field(default_factory=dict)
    addressed_to: str | None = None
    addressed_to_many: list[str] | None = None
    replies_to_event_id: str | None = None
    private_to_actors: list[str] | None = None


class V2CloseRequest(BaseModel):
    actor_handle: str
    resolution: str
    summary: str | None = None


@router.post("", status_code=201)
def open_operation(
    payload: V2OpenOperationRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """Open a new operation. Delegates to ChatConversationService.open_conversation
    in the chat-only era; the response surfaces the v2 operation id
    directly so callers never need the v1 conversation id."""
    services = request.app.state.services
    chat_service = services.chat_conversation_service
    # Lazy import keeps API module light at startup.
    from ..behaviors.chat.conversation_schemas import ConversationOpenRequest
    from ..behaviors.chat.conversation_service import (
        ChatThreadNotFoundError, ChatConversationStateError,
    )
    open_kwargs: dict[str, Any] = {
        "kind": payload.kind,
        "title": payload.title,
        "intent": payload.intent,
        "addressed_to": payload.addressed_to,
        "opener_actor": _strip_at(payload.opener_actor_handle),
        "objective": payload.objective,
    }
    if payload.success_criteria is not None:
        open_kwargs["success_criteria"] = payload.success_criteria
    try:
        summary = chat_service.open_conversation(
            discord_thread_id=payload.space_id,
            request=ConversationOpenRequest(**open_kwargs),
        )
    except ChatThreadNotFoundError:
        raise HTTPException(status_code=404, detail=f"space {payload.space_id} not found")
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    repo = V2Repository()
    with session_scope() as db:
        # summary.id is the v1 conversation id; resolve to v2 op id.
        from ..behaviors.chat.models import ChatConversationModel
        v1 = db.get(ChatConversationModel, summary.id)
        if v1 is None or v1.v2_operation_id is None:
            raise HTTPException(
                status_code=500,
                detail="open succeeded but v2 mirror missing -- check dual-write",
            )
        op = repo.get_operation(db, v1.v2_operation_id)
        return _serialize_operation(op, repo) | {"id": op.id}


@router.post("/{operation_id}/events", status_code=201)
def append_event(
    operation_id: str,
    payload: V2EventRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """Append a speech / lifecycle event to an operation. Currently the
    only kinds wired through this endpoint are speech.* (claim, question,
    proposal, etc.). Task-lifecycle and approval flows still go through
    /api/chat/conversations/.../task; G2 deliberately does not move
    those because the lease-token contract is non-trivial."""
    if not payload.kind.startswith("speech."):
        raise HTTPException(
            status_code=400,
            detail=(
                f"event kind {payload.kind!r} cannot be appended via this endpoint; "
                "only speech.* kinds are supported in G2 (task lifecycle stays "
                "on /api/chat for now)."
            ),
        )
    speech_kind = payload.kind.split(".", 1)[1]
    v1_id = _operation_to_v1_conversation_id(operation_id)
    services = request.app.state.services
    from ..behaviors.chat.conversation_schemas import SpeechActSubmitRequest
    from ..behaviors.chat.conversation_service import (
        ChatConversationNotFoundError, ChatConversationStateError, ChatActorIdentityError,
    )
    text = str(payload.payload.get("text", "") or payload.payload.get("content", ""))
    if not text:
        raise HTTPException(
            status_code=400,
            detail="payload.text (or payload.content) is required for speech.* events",
        )
    try:
        speech = services.chat_conversation_service.submit_speech(
            conversation_id=v1_id,
            request=SpeechActSubmitRequest(
                actor_name=_strip_at(payload.actor_handle),
                kind=speech_kind,
                content=text,
                addressed_to=payload.addressed_to,
                addressed_to_many=payload.addressed_to_many or [],
                replies_to_speech_id=None,  # v2 reply chain resolved below
                private_to_actors=payload.private_to_actors or [],
            ),
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="operation conversation not found")
    except ChatActorIdentityError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Resolve the just-written v1 message to its mirrored v2 event id.
    repo = V2Repository()
    with session_scope() as db:
        from ..behaviors.chat.models import ChatMessageModel
        msg = db.get(ChatMessageModel, speech.id)
        v2_event_id = msg.v2_event_id if msg else None
        # Wire reply chain after the fact when the caller passed v2 ids:
        # no v1 column exists for the v2 reply id, so the mirror copy
        # gets it by post-update.
        if payload.replies_to_event_id and v2_event_id:
            ev = db.get(OperationEventV2Model, v2_event_id)
            if ev is not None and ev.replies_to_event_id is None:
                ev.replies_to_event_id = payload.replies_to_event_id
                db.flush()
        if not v2_event_id:
            raise HTTPException(
                status_code=500,
                detail="speech accepted but v2 mirror missing -- check dual-write",
            )
        op = repo.get_operation(db, operation_id)
        ev = db.get(OperationEventV2Model, v2_event_id)
        return _serialize_event(ev, repo) | {"operation_id": op.id}


@router.post("/{operation_id}/close")
def close_operation(
    operation_id: str,
    payload: V2CloseRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    v1_id = _operation_to_v1_conversation_id(operation_id)
    services = request.app.state.services
    from ..behaviors.chat.conversation_service import (
        ChatConversationNotFoundError, ChatConversationStateError, ChatActorIdentityError,
    )
    try:
        services.chat_conversation_service.close_conversation(
            conversation_id=v1_id,
            closed_by=_strip_at(payload.actor_handle),
            resolution=payload.resolution,
            summary=payload.summary,
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="operation conversation not found")
    except ChatActorIdentityError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        return _serialize_operation(op, repo)
