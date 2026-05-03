"""Body size, JSON depth, and request timeout bounds.

These three knobs are the load-bearing perimeter for axis H
(adversarial robustness). The contract is:

  * Any request body whose Content-Length declares more than
    ``max_body_bytes`` is rejected with HTTP 413 BEFORE the body is
    read. Chunked uploads are checked as bytes accumulate.
  * Any handler that runs longer than ``request_timeout_s`` is
    cancelled and 504 is returned, except for explicitly-exempt path
    prefixes (SSE / long-poll).
  * Any free-form JSON object reaching the kernel must satisfy
    ``walk_json_depth(value, max_depth)`` or the helper raises
    ``ValueError``; callers convert to HTTP 400.

When ``log_only`` is True, a violation is logged but not enforced —
used during staged rollout. Phase 10's "surface first, enforce next"
pattern.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("opscure.security.bounds")


@dataclass(frozen=True, slots=True)
class BoundsConfig:
    max_body_bytes: int
    max_json_depth: int
    request_timeout_s: float
    log_only: bool
    timeout_exempt_prefixes: tuple[str, ...]


# ---------------------------------------------------------------------------
# JSON depth walker
# ---------------------------------------------------------------------------


def walk_json_depth(value: object, max_depth: int, *, _current: int = 0) -> None:
    """Raise ``ValueError`` if any dict/list nest exceeds ``max_depth``.

    Counts nesting of containers only; scalar leaves do not increment.
    A depth of 0 means the root must itself be a scalar.
    """
    if _current > max_depth:
        raise ValueError(
            f"json nesting depth > {max_depth} (free-form fields must stay "
            f"under the cap to keep parse cost bounded)"
        )
    if isinstance(value, dict):
        for v in value.values():
            walk_json_depth(v, max_depth, _current=_current + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            walk_json_depth(v, max_depth, _current=_current + 1)


# ---------------------------------------------------------------------------
# Body size — ASGI middleware (raw, not BaseHTTPMiddleware)
# ---------------------------------------------------------------------------
# BaseHTTPMiddleware buffers the body before dispatch — too late to
# enforce a cap. This is a raw ASGI middleware so it can short-circuit
# on Content-Length and wrap ``receive`` to count chunked uploads.


_BODY_TOO_LARGE_BODY = (
    b'{"detail":"request body exceeds the configured maximum",'
    b'"code":"body.too_large"}'
)


class BodySizeLimitMiddleware:
    """ASGI middleware. Enforces a per-request body byte cap.

    Three rejection paths:

      1. Content-Length header > cap → reject before reading body.
      2. Chunked / unknown length → wrap ``receive``; reject when
         accumulated bytes exceed the cap.
      3. ``log_only`` mode → log the violation and pass through.

    Non-HTTP scopes (websocket, lifespan) pass through unchanged.
    """

    def __init__(self, app, *, max_bytes: int, log_only: bool = False) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.log_only = log_only

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Content-Length pre-check
        declared: int | None = None
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break

        if declared is not None and declared > self.max_bytes:
            if self.log_only:
                logger.warning(
                    "body.too_large content_length=%d cap=%d path=%s (log_only)",
                    declared, self.max_bytes, scope.get("path"),
                )
                await self.app(scope, receive, send)
                return
            await self._reject(scope, send, declared)
            return

        # 2. Chunked / wrap receive to count
        if self.log_only:
            await self.app(scope, receive, send)
            return

        bytes_seen = 0
        rejected = False

        async def bounded_receive():
            nonlocal bytes_seen, rejected
            message = await receive()
            if rejected:
                # After rejection, force end-of-stream so the app
                # doesn't hang on a half-read body.
                return {"type": "http.request", "body": b"", "more_body": False}
            if message["type"] == "http.request":
                body = message.get("body", b"")
                bytes_seen += len(body)
                if bytes_seen > self.max_bytes:
                    rejected = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        # We can't easily 413 mid-stream after the app started, so
        # the strategy is: send a 413 directly here and never call the
        # app. We still need to drain receive to free the connection.
        # Track whether app has been called.
        app_started = False
        original_send = send

        async def guard_send(message):
            nonlocal app_started
            if rejected:
                # Suppress the app's response in favor of our 413.
                return
            app_started = True
            await original_send(message)

        await self.app(scope, bounded_receive, guard_send)

        if rejected and not app_started:
            await self._reject(scope, original_send, bytes_seen)
        elif rejected and app_started:
            # The app already started a response before the cap was
            # hit. Best we can do is log; the connection is in an
            # ambiguous state.
            logger.error(
                "body.too_large mid_stream after_response_started "
                "bytes=%d cap=%d path=%s",
                bytes_seen, self.max_bytes, scope.get("path"),
            )

    async def _reject(self, scope, send, observed: int) -> None:
        logger.warning(
            "body.too_large rejecting bytes=%s cap=%d path=%s",
            observed, self.max_bytes, scope.get("path"),
        )
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_BODY_TOO_LARGE_BODY)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": _BODY_TOO_LARGE_BODY,
            "more_body": False,
        })


# ---------------------------------------------------------------------------
# Request timeout — BaseHTTPMiddleware (handler runtime cap)
# ---------------------------------------------------------------------------


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Wraps each handler in ``asyncio.wait_for(timeout_s)``.

    Long-running endpoints (SSE inbox, websocket upgrades on /ws/*)
    are exempted by path prefix. Everything else has a hard ceiling
    so a slow-loris or stuck handler can't tie up a worker.
    """

    def __init__(
        self,
        app,
        *,
        timeout_s: float,
        exempt_prefixes: tuple[str, ...] = (),
        log_only: bool = False,
    ) -> None:
        super().__init__(app)
        self.timeout_s = timeout_s
        self.exempt_prefixes = exempt_prefixes
        self.log_only = log_only

    async def dispatch(self, request, call_next):
        path = request.url.path
        if self.exempt_prefixes and any(
            path.startswith(p) for p in self.exempt_prefixes
        ):
            return await call_next(request)

        if self.log_only:
            start = time.monotonic()
            response = await call_next(request)
            elapsed = time.monotonic() - start
            if elapsed > self.timeout_s:
                logger.warning(
                    "request.timeout exceeded elapsed=%.3fs cap=%.3fs path=%s (log_only)",
                    elapsed, self.timeout_s, path,
                )
            return response

        try:
            return await asyncio.wait_for(
                call_next(request), timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "request.timeout exceeded cap=%.3fs path=%s",
                self.timeout_s, path,
            )
            return JSONResponse(
                status_code=504,
                content={
                    "detail": (
                        f"request handler exceeded {self.timeout_s}s timeout"
                    ),
                    "code": "request.timeout",
                },
            )
