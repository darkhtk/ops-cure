"""H1: BodySizeLimitMiddleware, RequestTimeoutMiddleware, walk_json_depth."""
from __future__ import annotations

import asyncio
import sys

import pytest

from conftest import NAS_BRIDGE_ROOT


def _import():
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    from app.security.bounds import (
        BodySizeLimitMiddleware,
        RequestTimeoutMiddleware,
        walk_json_depth,
    )
    return {
        "BodySizeLimitMiddleware": BodySizeLimitMiddleware,
        "RequestTimeoutMiddleware": RequestTimeoutMiddleware,
        "walk_json_depth": walk_json_depth,
    }


# ---------------------------------------------------------------------------
# walk_json_depth
# ---------------------------------------------------------------------------


def test_walk_json_depth_scalar_root_passes_at_zero():
    m = _import()
    m["walk_json_depth"]("hello", max_depth=0)
    m["walk_json_depth"](42, max_depth=0)
    m["walk_json_depth"](None, max_depth=0)


def test_walk_json_depth_flat_dict_at_one():
    m = _import()
    m["walk_json_depth"]({"a": 1, "b": 2}, max_depth=1)


def test_walk_json_depth_rejects_over_cap():
    m = _import()
    nested = {"a": {"b": {"c": {"d": 1}}}}
    with pytest.raises(ValueError, match="json nesting depth"):
        m["walk_json_depth"](nested, max_depth=2)


def test_walk_json_depth_handles_lists():
    m = _import()
    deep_list = [[[[[]]]]]
    with pytest.raises(ValueError):
        m["walk_json_depth"](deep_list, max_depth=3)


def test_walk_json_depth_mixed_dict_list():
    m = _import()
    val = {"users": [{"profile": {"prefs": {"x": 1}}}]}
    # depth: dict(1) -> list(2) -> dict(3) -> dict(4) -> dict(5) -> leaf
    with pytest.raises(ValueError):
        m["walk_json_depth"](val, max_depth=4)
    m["walk_json_depth"](val, max_depth=5)  # exact fit


def test_walk_json_depth_empty_containers_count_one_level():
    m = _import()
    m["walk_json_depth"]({}, max_depth=0)  # empty dict has nothing to recurse
    m["walk_json_depth"]([], max_depth=0)


# ---------------------------------------------------------------------------
# BodySizeLimitMiddleware (ASGI)
# ---------------------------------------------------------------------------


class _SpyApp:
    """Records whether the inner app was reached."""

    def __init__(self) -> None:
        self.called = False
        self.received_body = b""

    async def __call__(self, scope, receive, send):
        self.called = True
        # Drain the body
        more = True
        while more:
            msg = await receive()
            self.received_body += msg.get("body", b"")
            more = msg.get("more_body", False)
        # Trivial 200 response
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})


async def _drive(mw, *, body: bytes, content_length: int | None):
    """Run the ASGI middleware against an HTTP scope."""
    headers: list[tuple[bytes, bytes]] = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v2/operations",
        "headers": headers,
    }
    sent: list[dict] = []

    async def receive():
        # Single-chunk delivery
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def test_body_size_within_cap_passes():
    m = _import()
    spy = _SpyApp()
    mw = m["BodySizeLimitMiddleware"](spy, max_bytes=1024)
    sent = asyncio.run(_drive(mw, body=b"x" * 100, content_length=100))
    assert spy.called is True
    assert sent[0]["status"] == 200


def test_body_size_content_length_over_cap_rejects_413():
    m = _import()
    spy = _SpyApp()
    mw = m["BodySizeLimitMiddleware"](spy, max_bytes=100)
    sent = asyncio.run(_drive(mw, body=b"x" * 200, content_length=200))
    assert spy.called is False, "app must not be reached when CL exceeds cap"
    assert sent[0]["status"] == 413
    assert b"body.too_large" in sent[1]["body"]


def test_body_size_chunked_over_cap_rejects():
    """No Content-Length header — must catch via byte counting."""
    m = _import()
    spy = _SpyApp()
    mw = m["BodySizeLimitMiddleware"](spy, max_bytes=50)
    sent = asyncio.run(_drive(mw, body=b"x" * 100, content_length=None))
    # App may or may not have been entered, but a 413 must surface.
    statuses = [msg["status"] for msg in sent if msg["type"] == "http.response.start"]
    assert 413 in statuses


def test_body_size_log_only_passes_through():
    m = _import()
    spy = _SpyApp()
    mw = m["BodySizeLimitMiddleware"](spy, max_bytes=10, log_only=True)
    sent = asyncio.run(_drive(mw, body=b"x" * 100, content_length=100))
    assert spy.called is True
    assert sent[0]["status"] == 200


def test_body_size_non_http_scope_passthrough():
    m = _import()
    spy = _SpyApp()
    mw = m["BodySizeLimitMiddleware"](spy, max_bytes=10)

    async def run():
        called = []

        async def fake_app(scope, receive, send):
            called.append(scope["type"])

        mw_ws = m["BodySizeLimitMiddleware"](fake_app, max_bytes=10)
        await mw_ws({"type": "websocket"}, lambda: None, lambda m: None)
        return called

    called = asyncio.run(run())
    assert called == ["websocket"]


def test_body_size_invalid_content_length_falls_back_to_counting():
    m = _import()
    spy = _SpyApp()
    mw = m["BodySizeLimitMiddleware"](spy, max_bytes=50)

    async def run():
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [(b"content-length", b"not-a-number")],
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"x" * 100, "more_body": False}

        async def send(msg):
            sent.append(msg)

        await mw(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert 413 in statuses


# ---------------------------------------------------------------------------
# RequestTimeoutMiddleware
# ---------------------------------------------------------------------------


def _build_starlette_app_with_timeout(timeout_s, exempt=(), log_only=False, sleep_s=0.0):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def slow(request):
        await asyncio.sleep(sleep_s)
        return PlainTextResponse("done")

    app = Starlette(routes=[
        Route("/slow", slow),
        Route("/v2/inbox/sse", slow),
    ])
    m = _import()
    app.add_middleware(
        m["RequestTimeoutMiddleware"],
        timeout_s=timeout_s,
        exempt_prefixes=exempt,
        log_only=log_only,
    )
    return app


def test_request_timeout_returns_504_on_overrun():
    from starlette.testclient import TestClient
    app = _build_starlette_app_with_timeout(timeout_s=0.05, sleep_s=0.5)
    with TestClient(app) as client:
        r = client.get("/slow")
    assert r.status_code == 504
    body = r.json()
    assert body["code"] == "request.timeout"


def test_request_timeout_under_cap_passes():
    from starlette.testclient import TestClient
    app = _build_starlette_app_with_timeout(timeout_s=1.0, sleep_s=0.01)
    with TestClient(app) as client:
        r = client.get("/slow")
    assert r.status_code == 200
    assert r.text == "done"


def test_request_timeout_exempt_prefix_not_enforced():
    from starlette.testclient import TestClient
    app = _build_starlette_app_with_timeout(
        timeout_s=0.05, sleep_s=0.2, exempt=("/v2/inbox/",),
    )
    with TestClient(app) as client:
        r = client.get("/v2/inbox/sse")
    assert r.status_code == 200, "exempt prefix must skip timeout"


def test_request_timeout_log_only_does_not_504():
    from starlette.testclient import TestClient
    app = _build_starlette_app_with_timeout(
        timeout_s=0.05, sleep_s=0.15, log_only=True,
    )
    with TestClient(app) as client:
        r = client.get("/slow")
    assert r.status_code == 200, "log_only mode must not enforce"
