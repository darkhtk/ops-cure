"""P12-1: contract.infer_implicit_responder pure-logic tests."""
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
    from app.kernel.v2 import contract
    return contract


def test_truly_terminal_returns_none():
    c = _import()
    assert c.infer_implicit_responder(
        expected_response=None,
        addressed_actor_ids=None,
        replies_to_author_actor_id=None,
    ) is None


def test_truly_terminal_empty_collections_returns_none():
    c = _import()
    assert c.infer_implicit_responder(
        expected_response={},
        addressed_actor_ids=[],
        replies_to_author_actor_id="",
    ) is None


def test_expected_response_handle_wins():
    c = _import()
    out = c.infer_implicit_responder(
        expected_response={"from_actor_handles": ["@curator", "@designer"]},
        addressed_actor_ids=["addr-actor"],
        replies_to_author_actor_id="reply-actor",
    )
    assert out == ("handle", "@curator")


def test_addressed_actor_id_when_no_expected_response():
    c = _import()
    out = c.infer_implicit_responder(
        expected_response=None,
        addressed_actor_ids=["addr-actor", "second"],
        replies_to_author_actor_id="reply-actor",
    )
    assert out == ("actor_id", "addr-actor")


def test_replies_to_author_when_no_expected_no_addressed():
    c = _import()
    out = c.infer_implicit_responder(
        expected_response=None,
        addressed_actor_ids=None,
        replies_to_author_actor_id="reply-actor",
    )
    assert out == ("actor_id", "reply-actor")


def test_expected_response_with_no_handles_falls_through():
    c = _import()
    out = c.infer_implicit_responder(
        expected_response={"from_actor_handles": []},
        addressed_actor_ids=["addr-actor"],
        replies_to_author_actor_id=None,
    )
    assert out == ("actor_id", "addr-actor")


def test_expected_response_with_kinds_only_falls_through():
    """from_actor_handles missing entirely — declared kinds shouldn't
    rescue an empty addressee list."""
    c = _import()
    out = c.infer_implicit_responder(
        expected_response={"kinds": ["ratify"]},
        addressed_actor_ids=None,
        replies_to_author_actor_id="reply-actor",
    )
    assert out == ("actor_id", "reply-actor")


def test_event_system_nudge_kind_string():
    c = _import()
    assert c.EVENT_SYSTEM_NUDGE == "chat.system.nudge"


def test_event_system_nudge_not_in_speech_kinds():
    """Nudge MUST NOT be a speech kind — it's a routing signal, not a turn."""
    c = _import()
    bare = c.EVENT_SYSTEM_NUDGE.split(".")[-1]
    assert bare not in c.SPEECH_KINDS, (
        "system.nudge must stay outside SPEECH_KINDS so it doesn't count "
        "toward max_rounds or over_speech"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
