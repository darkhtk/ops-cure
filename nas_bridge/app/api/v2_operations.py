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

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import BridgeCaller, require_bridge_caller
from ..db import session_scope
from ..kernel.v2 import V2Repository

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
