"""Conformance suite fixtures.

A single ``client`` fixture that is either:

- an httpx-style adapter over a FastAPI TestClient (in-process), or
- a real httpx.Client against ``BRIDGE_CONFORMANCE_BASE_URL``.

Both surface the same minimum interface: ``get(url, ...)``,
``post(url, ...)``, ``stream(method, url, ...)`` returning an object
with ``.status_code``, ``.headers``, ``.json()``, ``.text``.

Tests don't need to know which mode is active.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

CONFTEST_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.normpath(os.path.join(CONFTEST_DIR, "..", ".."))
NAS_BRIDGE_ROOT = os.path.join(PROJECT_ROOT, "nas_bridge")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "conformance_required: a passing impl MUST pass these tests",
    )
    config.addinivalue_line(
        "markers",
        "conformance_optional: behavior may legitimately vary per impl",
    )


def _shared_token() -> str:
    return os.environ.get("BRIDGE_SHARED_AUTH_TOKEN", "conformance-shared")


def _live_base_url() -> str | None:
    raw = os.environ.get("BRIDGE_CONFORMANCE_BASE_URL", "").strip()
    return raw or None


@pytest.fixture(scope="session")
def base_url() -> str | None:
    return _live_base_url()


@pytest.fixture
def client(tmp_path, monkeypatch, base_url):  # noqa: ANN001
    """Conformance HTTP client — adapter over either in-process
    TestClient or live httpx.Client. Auth header preconfigured.

    Note: the ``X-Actor-Token`` and ``traceparent`` headers are NOT
    pre-set here; tests that exercise them set them per-call.
    """
    token = _shared_token()
    if base_url is None:
        # In-process mode: spin up our bridge in a tmpdir so each
        # test starts from a clean DB.
        monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", token)
        monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
        monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'conf.db').as_posix()}")
        monkeypatch.setenv("BRIDGE_POLICY_SWEEPER_SECONDS", "0")
        monkeypatch.setenv("BRIDGE_TEST_MODE", "1")
        if NAS_BRIDGE_ROOT not in sys.path:
            sys.path.insert(0, NAS_BRIDGE_ROOT)
        for m in list(sys.modules):
            if m == "app" or m.startswith("app."):
                del sys.modules[m]
        import app.config as config
        config.get_settings.cache_clear()
        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app
        import app.db as db
        db.init_db()
        with TestClient(fastapi_app) as c:
            c.headers.update({"Authorization": f"Bearer {token}"})
            yield c
    else:
        import httpx
        with httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as c:
            yield c


@pytest.fixture
def space_id(client) -> str:
    """Fresh thread for the test, via the test-mode fixture endpoint."""
    r = client.post("/v2/_test/provision-thread")
    if r.status_code == 404:
        pytest.skip(
            "BRIDGE_TEST_MODE=1 is not set on the bridge under test — "
            "fixture endpoint /v2/_test/provision-thread is disabled. "
            "Live runs must enable test mode."
        )
    assert r.status_code == 201, r.text
    return r.json()["space_id"]


@pytest.fixture
def issue_token(client):
    """Helper: issue a per-actor token. Returns a callable."""
    def _issue(handle: str, scope: str = "admin", label: str | None = None) -> str:
        r = client.post(
            f"/v2/actors/{handle.lstrip('@')}/tokens",
            json={"scope": scope, "label": label},
        )
        assert r.status_code == 201, r.text
        return r.json()["token"]
    return _issue
