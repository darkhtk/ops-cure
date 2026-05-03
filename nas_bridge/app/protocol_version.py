"""v3 phase 4 — protocol version negotiation.

Centralizes:

  * the supported protocol versions the bridge currently speaks
  * a FastAPI middleware that:
      - parses ``X-Protocol-Version`` from incoming requests
      - rejects unknown versions with HTTP 400
      - echoes the supported versions list as ``X-Protocol-Version-Supported``
        on every response
  * a per-request usage counter so we can see who's still on which
    version before retiring one

Major-bump rule (decided this cycle, written here as the normative
source for future contributors):

  A change is **major** (bumps to e.g. v4) when it:
    - removes an existing field
    - adds a *required* field on an existing endpoint
    - changes the semantics of an existing field
    - removes a value from an existing enum

  A change is **minor** (e.g. v3.0 → v3.1) when it:
    - adds a new optional field
    - adds a new value to an existing enum
    - adds a new endpoint

Clients that do not send ``X-Protocol-Version`` get the default
``CURRENT_VERSION`` semantics (no breakage on existing callers).
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("opscure.protocol_version")

SUPPORTED_VERSIONS: Final[tuple[str, ...]] = ("3.0", "3.1")
CURRENT_VERSION: Final[str] = "3.1"

HEADER_REQUEST = "X-Protocol-Version"
HEADER_RESPONSE_SUPPORTED = "X-Protocol-Version-Supported"
HEADER_RESPONSE_CURRENT = "X-Protocol-Version-Current"

_NEGOTIABLE_PREFIXES = ("/v2/", "/v3/")

# Module-level usage counter. Read via ``usage_counts()``; reset by
# overwriting the dict (test isolation).
_USAGE_COUNTER: Counter[str] = Counter()


def usage_counts() -> dict[str, int]:
    return dict(_USAGE_COUNTER)


def _reset_for_tests() -> None:
    """Hook tests can call to drop accumulated counts."""
    _USAGE_COUNTER.clear()


def _negotiation_headers() -> dict[str, str]:
    return {
        HEADER_RESPONSE_SUPPORTED: ", ".join(SUPPORTED_VERSIONS),
        HEADER_RESPONSE_CURRENT: CURRENT_VERSION,
    }


class ProtocolVersionMiddleware(BaseHTTPMiddleware):
    """Enforce + advertise protocol version on every HTTP request."""

    async def dispatch(self, request, call_next):  # noqa: ANN001
        path = request.url.path
        version_raw = request.headers.get(HEADER_REQUEST, "").strip()
        is_protocol_path = any(path.startswith(p) for p in _NEGOTIABLE_PREFIXES)

        if version_raw and version_raw not in SUPPORTED_VERSIONS:
            logger.warning(
                "rejecting request with unsupported %s=%s path=%s",
                HEADER_REQUEST, version_raw, path,
            )
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"unsupported {HEADER_REQUEST}={version_raw!r}; "
                        f"supported={list(SUPPORTED_VERSIONS)}"
                    ),
                    "supported": list(SUPPORTED_VERSIONS),
                    "current": CURRENT_VERSION,
                },
                headers=_negotiation_headers(),
            )

        require_header = os.environ.get(
            "BRIDGE_REQUIRE_PROTOCOL_VERSION", "",
        ).strip().lower() in {"1", "true", "yes", "on"}
        if require_header and is_protocol_path and not version_raw:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"{HEADER_REQUEST} header is required "
                        f"(BRIDGE_REQUIRE_PROTOCOL_VERSION=1)"
                    ),
                    "supported": list(SUPPORTED_VERSIONS),
                    "current": CURRENT_VERSION,
                },
                headers=_negotiation_headers(),
            )

        if is_protocol_path:
            seen = version_raw or CURRENT_VERSION
            _USAGE_COUNTER[seen] += 1

        response = await call_next(request)
        for key, value in _negotiation_headers().items():
            response.headers[key] = value
        return response
