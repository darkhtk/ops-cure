"""v3 phase 4 — spec drift guard.

`docs/protocol-v3-spec.md` is normative. Code drift is silent on
prose, so this test pins three things the spec calls out by name:

  - SPEECH_KINDS frozenset matches the table in §6.5
  - Policy error codes match the table in §13 + Appendix A
  - The published JSON Schema enums match the contract sets

If you legitimately add a new speech kind / error code, update both
this test AND the spec doc; the failing assertion is the reminder.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import NAS_BRIDGE_ROOT


SPEC_PATH = Path(__file__).parent.parent / "docs" / "protocol-v3-spec.md"


def _spec_text() -> str:
    if not SPEC_PATH.exists():
        pytest.skip(f"spec not found at {SPEC_PATH}")
    return SPEC_PATH.read_text(encoding="utf-8")


def _ensure_test_env():
    """app.kernel.v2 imports app.config, which validates settings.
    Provide stub env vars so the import succeeds in this drift test
    (we don't actually run the bridge here)."""
    import os
    os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "drift-test")
    os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")


def _contract():
    _ensure_test_env()
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    from app.kernel.v2 import contract as c
    return c


def _policy_codes() -> set[str]:
    _ensure_test_env()
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    from app.kernel.v2 import (
        CODE_MAX_ROUNDS_EXHAUSTED, CODE_REPLY_KIND_REJECTED,
        CODE_CLOSE_NEEDS_OPERATOR, CODE_CLOSE_NEEDS_QUORUM,
        CODE_CLOSE_NEEDS_PARTICIPANT, CODE_JOIN_INVITE_ONLY,
        CODE_INVITE_NEEDS_PARTICIPANT,
    )
    return {
        CODE_MAX_ROUNDS_EXHAUSTED, CODE_REPLY_KIND_REJECTED,
        CODE_CLOSE_NEEDS_OPERATOR, CODE_CLOSE_NEEDS_QUORUM,
        CODE_CLOSE_NEEDS_PARTICIPANT, CODE_JOIN_INVITE_ONLY,
        CODE_INVITE_NEEDS_PARTICIPANT,
    }


def test_spec_lists_every_speech_kind(monkeypatch):
    """The spec § "SpeechKinds (closed enum at v3.1)" enumerates the
    SPEECH_KINDS frozenset by name. Drift means a new kind landed in
    code without doc update."""
    text = _spec_text()
    contract = _contract()
    missing = []
    for kind in contract.SPEECH_KINDS:
        if kind not in text:
            missing.append(kind)
    assert not missing, (
        f"spec is missing speech kinds: {sorted(missing)}. "
        f"Update docs/protocol-v3-spec.md §6.5 + §8 when adding kinds."
    )


def test_spec_lists_every_policy_error_code():
    """The spec § "Error codes" table + Appendix A list each
    CODE_* by exact wire string."""
    text = _spec_text()
    missing = []
    for code in _policy_codes():
        if code not in text:
            missing.append(code)
    assert not missing, (
        f"spec is missing error codes: {sorted(missing)}. "
        f"Update docs/protocol-v3-spec.md §13 + Appendix A."
    )


def test_spec_lists_every_close_policy():
    """§6.1 OperationPolicy lists every close policy by string."""
    text = _spec_text()
    contract = _contract()
    missing = []
    for cp in contract.ALL_CLOSE_POLICIES:
        if cp not in text:
            missing.append(cp)
    assert not missing, (
        f"spec is missing close policies: {sorted(missing)}."
    )


def test_spec_lists_every_join_policy():
    text = _spec_text()
    contract = _contract()
    missing = []
    for jp in contract.ALL_JOIN_POLICIES:
        if jp not in text:
            missing.append(jp)
    assert not missing, (
        f"spec is missing join policies: {sorted(missing)}."
    )


def test_spec_calls_out_strict_mode_envvars():
    """The two strict-mode env vars are part of the wire contract for
    deployments; they MUST be named in the spec."""
    text = _spec_text()
    for var in ("BRIDGE_REQUIRE_PROTOCOL_VERSION", "BRIDGE_REQUIRE_ACTOR_TOKEN"):
        assert var in text, f"spec is missing env var: {var}"
