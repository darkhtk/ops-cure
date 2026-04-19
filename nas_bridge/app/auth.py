from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings


def require_bridge_token(
    authorization: str = Header(default=""),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = f"Bearer {settings.shared_auth_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bridge authorization token.",
        )
