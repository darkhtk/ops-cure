"""v3 phase 4 — W3C traceparent propagation.

Minimal end-to-end trace for the bridge:

  - parse incoming ``traceparent`` (W3C format) per request
  - generate one if absent (so all logs have a trace id)
  - put the active trace_id / span_id on a contextvar
  - echo ``traceparent`` on every response so callers can correlate
  - log records carry ``trace_id`` / ``span_id`` via a logging filter

Out of scope (deferred):

  - OTLP export to Jaeger/Tempo/Loki
  - distributed span hierarchies (we have one logical span per HTTP
    request -- enough for "what happened to this request" queries)
  - propagating into the claude CLI process (claude doesn't respect
    traceparent on its own; agent_loop forwards across SSE↔POST so
    the bridge↔agent boundary is connected, which covers ~80% of the
    "where did this op event come from" question)
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("opscure.trace")

HEADER = "traceparent"

# W3C traceparent: ``00-<32hex trace_id>-<16hex span_id>-<2hex flags>``
_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


# Context vars for the active request's trace identifiers. Read by
# the logging filter to enrich every log record.
_TRACE_ID: ContextVar[str | None] = ContextVar("opscure_trace_id", default=None)
_SPAN_ID: ContextVar[str | None] = ContextVar("opscure_span_id", default=None)


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def current_span_id() -> str | None:
    return _SPAN_ID.get()


def _new_trace_id() -> str:
    return secrets.token_hex(16)  # 32 hex chars = 128 bits


def _new_span_id() -> str:
    return secrets.token_hex(8)   # 16 hex chars = 64 bits


def _format_traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


def _parse_traceparent(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    m = _PATTERN.match(value.strip().lower())
    if not m:
        return None
    return m.group(1), m.group(2)


class TraceparentMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        incoming = request.headers.get(HEADER)
        parsed = _parse_traceparent(incoming)
        if parsed is None:
            trace_id = _new_trace_id()
        else:
            trace_id = parsed[0]
        # Always allocate a fresh span_id per HTTP request. The parent
        # span (when present) is in incoming; we don't propagate parent
        # here because we don't ship a span tree in this minimal cut.
        span_id = _new_span_id()

        token_t = _TRACE_ID.set(trace_id)
        token_s = _SPAN_ID.set(span_id)
        try:
            # Emit a single span-entry log so trace_id/span_id appear in
            # logs even for handlers that don't log themselves. Cheap,
            # one record per request, makes "where's this trace" queries
            # actually answerable. Pass trace_id/span_id via ``extra``
            # so they're on the LogRecord regardless of whether the
            # filter has been installed.
            logger.debug(
                "request span open: %s %s",
                request.method, request.url.path,
                extra={"trace_id": trace_id, "span_id": span_id},
            )
            response = await call_next(request)
        finally:
            _TRACE_ID.reset(token_t)
            _SPAN_ID.reset(token_s)
        response.headers[HEADER] = _format_traceparent(trace_id, span_id)
        return response


class TraceContextLogFilter(logging.Filter):
    """Logging filter that copies the current trace_id / span_id onto
    every log record. Use with a formatter that includes
    ``%(trace_id)s`` / ``%(span_id)s`` to surface them.

    Records emitted outside an HTTP request (startup, sweepers) get
    ``trace_id="-"`` so the format string never blows up on missing
    keys.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.trace_id = _TRACE_ID.get() or "-"
        record.span_id = _SPAN_ID.get() or "-"
        return True


def install_logging_filter() -> None:
    """Attach the trace-context filter to the root logger so any logger
    that propagates up gets enriched. Idempotent."""
    root = logging.getLogger()
    for f in root.filters:
        if isinstance(f, TraceContextLogFilter):
            return
    root.addFilter(TraceContextLogFilter())


def trace_format_enabled() -> bool:
    """Honor BRIDGE_LOG_TRACE_IDS to opt into trace-id-bearing format
    string. When false, the filter still attaches ids but the
    formatter doesn't print them."""
    return os.environ.get("BRIDGE_LOG_TRACE_IDS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
