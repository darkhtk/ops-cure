from __future__ import annotations

import logging
import re
from collections.abc import Callable

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from .config import Settings, get_settings

logger = logging.getLogger("opscure.auth")

DEFAULT_BRIDGE_PERMISSIONS = (
    "bridge:read",
    "bridge:write",
    "bridge:stream",
    "bridge:control",
)
_CLIENT_ID_SANITIZER = re.compile(r"[^A-Za-z0-9._:-]+")
_MAX_CLIENT_ID_LENGTH = 64


class BridgeCaller(BaseModel):
    model_config = ConfigDict(frozen=True)

    auth_method: str = "bridge-shared-bearer"
    subject: str = "shared-bridge-token"
    permissions: tuple[str, ...] = DEFAULT_BRIDGE_PERMISSIONS
    asserted_client_id: str | None = None

    def has_permissions(self, *required: str) -> bool:
        permission_set = set(self.permissions)
        return all(permission in permission_set for permission in required)


def _client_host(request: Request) -> str:
    client = getattr(request, "client", None)
    return client.host if client is not None and client.host else "-"


def _normalize_asserted_client_id(value: str) -> str | None:
    text = _CLIENT_ID_SANITIZER.sub("", str(value or "").strip())
    if not text:
        return None
    return text[:_MAX_CLIENT_ID_LENGTH]


def _raise_auth_error(*, request: Request, detail: str, status_code: int) -> None:
    logger.warning(
        "bridge auth denied method=%s path=%s client=%s detail=%s",
        request.method,
        request.url.path,
        _client_host(request),
        detail,
    )
    raise HTTPException(status_code=status_code, detail=detail)


def build_bridge_audit_fields(caller: BridgeCaller) -> dict[str, str | None]:
    return {
        "authMethod": caller.auth_method,
        "subject": caller.subject,
        "assertedClientId": caller.asserted_client_id,
    }


def require_bridge_caller(
    request: Request,
    authorization: str = Header(default=""),
    x_bridge_client_id: str = Header(default=""),
    settings: Settings = Depends(get_settings),
) -> BridgeCaller:
    expected = f"Bearer {settings.shared_auth_token}"
    if authorization != expected:
        _raise_auth_error(
            request=request,
            detail="Invalid bridge authorization token.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    caller = BridgeCaller(
        asserted_client_id=_normalize_asserted_client_id(x_bridge_client_id),
    )
    logger.debug(
        "bridge auth granted method=%s path=%s client=%s caller=%s",
        request.method,
        request.url.path,
        _client_host(request),
        caller.asserted_client_id or caller.subject,
    )
    return caller


def require_bridge_token(
    caller: BridgeCaller = Depends(require_bridge_caller),
) -> BridgeCaller:
    return caller


def require_bridge_permissions(*required_permissions: str) -> Callable[..., BridgeCaller]:
    def dependency(
        request: Request,
        caller: BridgeCaller = Depends(require_bridge_caller),
    ) -> BridgeCaller:
        if caller.has_permissions(*required_permissions):
            return caller
        missing = sorted(set(required_permissions) - set(caller.permissions))
        _raise_auth_error(
            request=request,
            detail=f"Missing bridge permission: {', '.join(missing)}",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    return dependency


# ---- v3 phase 3.x — per-actor token authentication --------------------

import hashlib as _hashlib
import os as _os


def hash_actor_token(plaintext: str) -> str:
    """Stable token-hash function used by both issue and verify paths.

    SHA-256 hex. Stored verbatim in ``actor_tokens_v2.token_hash``. The
    plaintext token never reaches the DB or logs.
    """
    return _hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _resolve_actor_token(token: str | None) -> tuple[str | None, str | None]:
    """If ``token`` resolves to a non-revoked ``actor_tokens_v2`` row,
    return ``(actor_handle, scope)``. Otherwise ``(None, None)``.

    Imported lazily so the ``app.auth`` module stays cheap to import
    in environments that don't use v2 at all.
    """
    if not token:
        return None, None
    from .db import session_scope
    from .kernel.v2 import V2Repository
    from .kernel.v2.models import ActorV2Model
    repo = V2Repository()
    with session_scope() as db:
        bound = repo.get_actor_token_by_hash(db, token_hash=hash_actor_token(token))
        if bound is None:
            return None, None
        actor = db.get(ActorV2Model, bound.actor_id)
        if actor is None:
            return None, None
        return actor.handle, bound.scope


def _resolve_actor_token_handle(token: str | None) -> str | None:
    """Back-compat wrapper around ``_resolve_actor_token`` for callers
    that only need the handle (no scope decision)."""
    handle, _ = _resolve_actor_token(token)
    return handle


# v3 phase 4 token scopes. Stable wire vocabulary.
TOKEN_SCOPE_ADMIN = "admin"
TOKEN_SCOPE_SPEAK = "speak"
TOKEN_SCOPE_READ_ONLY = "read-only"
ALL_TOKEN_SCOPES: frozenset[str] = frozenset({
    TOKEN_SCOPE_ADMIN, TOKEN_SCOPE_SPEAK, TOKEN_SCOPE_READ_ONLY,
})

# Required scope per protocol "verb category". A token must carry a
# scope >= the required one for the operation to be admissible.
_SCOPE_RANK = {
    TOKEN_SCOPE_READ_ONLY: 1,
    TOKEN_SCOPE_SPEAK: 2,
    TOKEN_SCOPE_ADMIN: 3,
}


def _scope_satisfies(actual: str, needed: str) -> bool:
    return _SCOPE_RANK.get(actual, 0) >= _SCOPE_RANK.get(needed, 99)


def require_scope(
    request: Request,
    *,
    x_actor_token: str | None,
    needed: str,
) -> None:
    """Reject a request whose token scope is below the required level.

    No-op when ``x_actor_token`` is missing — legacy / shared-bearer
    flow stays unchanged. The token-bearing path gets the additional
    enforcement so a ``read-only`` token cannot mutate even if the
    handle would otherwise be authorized.
    """
    if not x_actor_token:
        return
    _, scope = _resolve_actor_token(x_actor_token)
    if scope is None:
        # invalid/revoked tokens are caught by verify_actor_handle_claim;
        # don't double-error here.
        return
    if not _scope_satisfies(scope, needed):
        _raise_auth_error(
            request=request,
            detail=(
                f"X-Actor-Token scope={scope!r} insufficient; "
                f"this endpoint needs scope>={needed!r}"
            ),
            status_code=status.HTTP_403_FORBIDDEN,
        )


def verify_actor_handle_claim(
    request: Request,
    *,
    claimed_handle: str,
    x_actor_token: str | None,
) -> None:
    """Reject a request whose claimed actor handle disagrees with the
    handle bound to ``X-Actor-Token``.

    Three modes (from least to most strict):

    1. **No X-Actor-Token at all.** When ``BRIDGE_REQUIRE_ACTOR_TOKEN``
       is unset / falsy: legacy mode, we accept the claimed handle
       (bridge auth is the shared bearer the dependency already
       checked). This is the deployment story today.
    2. **No X-Actor-Token but BRIDGE_REQUIRE_ACTOR_TOKEN=1**: rejected.
       Hard mode for production exposure.
    3. **X-Actor-Token present**: we always verify. If the token
       resolves to handle X but the request claims Y, reject with 403.
       This catches the squatting case even in legacy mode.
    """
    require = _os.environ.get("BRIDGE_REQUIRE_ACTOR_TOKEN", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not x_actor_token:
        if require:
            _raise_auth_error(
                request=request,
                detail="X-Actor-Token header required (BRIDGE_REQUIRE_ACTOR_TOKEN=1)",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return  # legacy path: trust the shared bearer
    bound_handle = _resolve_actor_token_handle(x_actor_token)
    if bound_handle is None:
        _raise_auth_error(
            request=request,
            detail="X-Actor-Token is invalid or revoked",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    claimed_norm = claimed_handle if claimed_handle.startswith("@") else f"@{claimed_handle}"
    if bound_handle != claimed_norm:
        _raise_auth_error(
            request=request,
            detail=(
                f"X-Actor-Token is bound to {bound_handle!r}, "
                f"cannot speak as {claimed_norm!r}"
            ),
            status_code=status.HTTP_403_FORBIDDEN,
        )
