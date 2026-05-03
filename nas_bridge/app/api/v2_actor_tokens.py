"""v3 phase 3.x — per-actor token issuance + revocation.

Token lifecycle:

  POST   /v2/actors/{handle}/tokens          create + return plaintext ONCE
  GET    /v2/actors/{handle}/tokens          list (metadata only, no plaintext)
  POST   /v2/actors/{handle}/tokens/{id}/revoke   soft-revoke

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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import BridgeCaller, hash_actor_token, require_bridge_caller
from ..db import session_scope
from ..kernel.v2 import V2Repository
from ..kernel.v2.actor_service import ActorService

router = APIRouter(prefix="/v2/actors", tags=["v2-actor-tokens", "protocol-v3-public"])


class IssueTokenRequest(BaseModel):
    label: str | None = None


class IssueTokenResponse(BaseModel):
    id: str
    actor_handle: str
    token: str          # plaintext, returned ONCE
    label: str | None = None
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
        )
        return {
            "id": row.id,
            "actor_handle": handle_norm,
            "token": plaintext,
            "label": row.label,
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
                "created_at": r.created_at.isoformat(),
                "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            }
            for r in rows
        ]
    return {"actor_handle": handle_norm, "tokens": items}


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
