"""P12-3: _compute_effective_addr — three-channel turn-taking source."""
from __future__ import annotations

import os
import sys

import pytest

from conftest import NAS_BRIDGE_ROOT


def _import():
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
    os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
    from app.behaviors.chat.conversation_service import _compute_effective_addr
    return _compute_effective_addr


def test_primary_addr_wins():
    f = _import()
    assert f(primary_addr="@alice", expected_response={"from_actor_handles": ["@bob"]}) == "@alice"


def test_expected_response_fallback_when_no_primary():
    f = _import()
    assert f(primary_addr=None, expected_response={"from_actor_handles": ["@bob"]}) == "@bob"


def test_no_signal_returns_none_terminal():
    f = _import()
    assert f(primary_addr=None, expected_response=None) is None


def test_empty_primary_falls_through_to_expected_response():
    f = _import()
    assert f(primary_addr="", expected_response={"from_actor_handles": ["@curator"]}) == "@curator"


def test_expected_response_with_no_handles_returns_none():
    f = _import()
    assert f(primary_addr=None, expected_response={"from_actor_handles": []}) is None
    assert f(primary_addr=None, expected_response={"kinds": ["ratify"]}) is None


def test_expected_response_first_handle_used():
    f = _import()
    assert f(
        primary_addr=None,
        expected_response={"from_actor_handles": ["@curator", "@designer"]},
    ) == "@curator"


def test_expected_response_non_dict_ignored():
    f = _import()
    assert f(primary_addr=None, expected_response="not-a-dict") is None
    assert f(primary_addr=None, expected_response=42) is None


def test_handle_must_be_string():
    f = _import()
    # Non-string first element falls through to None
    assert f(primary_addr=None, expected_response={"from_actor_handles": [42]}) is None
    assert f(primary_addr=None, expected_response={"from_actor_handles": [None]}) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
