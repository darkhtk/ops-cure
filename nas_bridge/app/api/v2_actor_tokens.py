"""v3 phase 3.x — per-actor token issuance + revocation.

Token lifecycle:

  POST   /v2/actors/{handle}/tokens          create + return plaintext ONCE
  GET    /v2/actors/{handle}/tokens          list (metadata only, no plaintext)
  POST   /v2/actors/{handle}/tokens/{id}/revoke   soft-revoke

  POST   /v2/actors/{handle}/heartbeat       (phase 4) liveness ping;
                                              updates actors_v2.last_seen_at

All endpoints require the shared admin bearer (the existing
``require_bridge_caller`` dependency); per-actor tokens cannot mint
themselves. Plaintext is shown on issue and never again.

The auth check that *consumes* these tokens is in ``app.auth``
(see ``verify_actor_handle_claim``). Endpoints that take a
claimed actor handle (POST /v2/operations/{id}/events, /close, etc.)
call that helper to enforce token↔handle binding.
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ..auth import (
    ALL_TOKEN_SCOPES, BridgeCaller, TOKEN_SCOPE_ADMIN,
    hash_actor_token, require_bridge_caller, verify_actor_handle_claim,
)
from ..db import session_scope
from ..kernel.v2 import V2Repository
from ..kernel.v2.actor_service import ActorService

router = APIRouter(prefix="/v2/actors", tags=["v2-actor-tokens", "protocol-v3-public"])


class IssueTokenRequest(BaseModel):
    label: str | None = None
    scope: str = TOKEN_SCOPE_ADMIN


class IssueTokenResponse(BaseModel):
    id: str
    actor_handle: str
    token: str          # plaintext, returned ONCE
    label: str | None = None
    scope: str
    created_at: str


class TokenSummary(BaseModel):
    id: str
    actor_handle: str
    label: str | None = None
    created_at: str
    revoked_at: str | None = None


def _normalize(handle: str) -> str:
    return handle if handle.startswith("@") else f"@{handle}"


@router.post("/{handle}/tokens", response_model=IssueTokenResponse, status_code=201)
def issue_actor_token(
    handle: str,
    payload: IssueTokenRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """Mint a new token bound to ``actor_handle``. Auto-provisions the
    actor row if absent so first-issue + first-subscribe land on the
    same identity. Returns the plaintext token *once* — store it on
    the agent side immediately."""
    handle_norm = _normalize(handle)
    if payload.scope not in ALL_TOKEN_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scope {payload.scope!r}; valid: {sorted(ALL_TOKEN_SCOPES)}",
        )
    repo = V2Repository()
    plaintext = secrets.token_urlsafe(48)
    token_hash = hash_actor_token(plaintext)
    with session_scope() as db:
        actor = ActorService(repo).ensure_actor_by_handle(
            db, handle=handle_norm, display_name=handle.lstrip("@"), kind="ai",
        )
        row = repo.create_actor_token(
            db,
            actor_id=actor.id,
            token_hash=token_hash,
            label=payload.label,
            scope=payload.scope,
        )
        return {
            "id": row.id,
            "actor_handle": handle_norm,
            "token": plaintext,
            "label": row.label,
            "scope": row.scope,
            "created_at": row.created_at.isoformat(),
        }


@router.get("/{handle}/tokens")
def list_actor_tokens(
    handle: str,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    handle_norm = _normalize(handle)
    repo = V2Repository()
    with session_scope() as db:
        actor = repo.get_actor_by_handle(db, handle_norm)
        if actor is None:
            return {"actor_handle": handle_norm, "tokens": []}
        rows = repo.list_actor_tokens(db, actor_id=actor.id)
        items = [
            {
                "id": r.id,
                "actor_handle": handle_norm,
                "label": r.label,
                "scope": r.scope,
                "created_at": r.created_at.isoformat(),
                "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            }
            for r in rows
        ]
    return {"actor_handle": handle_norm, "tokens": items}


@router.post("/{handle}/heartbeat")
def actor_heartbeat(
    handle: str,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
    x_actor_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """v3 phase 4 — liveness ping.

    Updates ``actors_v2.last_seen_at`` so other participants (and
    future presence sweepers) can tell the difference between "agent
    quiet because nothing to say" and "agent dead". Auto-provisions
    the actor row if absent — a fresh agent's first heartbeat doubles
    as registration.
    """
    handle_norm = _normalize(handle)
    verify_actor_handle_claim(
        request, claimed_handle=handle_norm, x_actor_token=x_actor_token,
    )
    repo = V2Repository()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        actor = ActorService(repo).ensure_actor_by_handle(
            db, handle=handle_norm, display_name=handle.lstrip("@"), kind="ai",
        )
        repo.update_actor_presence(
            db, actor_id=actor.id, status="online", last_seen_at=now,
        )
        return {
            "actor_handle": handle_norm,
            "last_seen_at": now.isoformat(),
        }


@router.post("/{handle}/tokens/{token_id}/revoke")
def revoke_actor_token(
    handle: str,
    token_id: str,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    handle_norm = _normalize(handle)
    repo = V2Repository()
    with session_scope() as db:
        actor = repo.get_actor_by_handle(db, handle_norm)
        if actor is None:
            raise HTTPException(status_code=404, detail=f"actor {handle_norm} not found")
        from ..kernel.v2.models import ActorTokenV2Model
        row = db.get(ActorTokenV2Model, token_id)
        if row is None or row.actor_id != actor.id:
            raise HTTPException(status_code=404, detail="token not found for actor")
        revoked = repo.revoke_actor_token(db, token_id=token_id)
        return {
            "id": revoked.id,
            "actor_handle": handle_norm,
            "revoked_at": revoked.revoked_at.isoformat() if revoked.revoked_at else None,
        }
