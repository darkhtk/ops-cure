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
