"""F10: per-kind state machine validation."""
from __future__ import annotations

import pytest

import os
import sys

from conftest import NAS_BRIDGE_ROOT


def _import():
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
    os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
    from app.kernel.v2 import (
        OperationStateMachine, StateMachineError,
        KIND_GENERAL, KIND_INQUIRY, KIND_PROPOSAL, KIND_TASK,
        STATE_OPEN, STATE_CLAIMED, STATE_EXECUTING,
        STATE_BLOCKED_APPROVAL, STATE_VERIFYING, STATE_CLOSED,
    )
    return locals()


def test_inquiry_close_only_allows_inquiry_resolutions():
    mod = _import()
    m = mod["OperationStateMachine"]()
    assert m.can_close(kind=mod["KIND_INQUIRY"], from_state=mod["STATE_OPEN"], resolution="answered").allowed
    assert m.can_close(kind=mod["KIND_INQUIRY"], from_state=mod["STATE_OPEN"], resolution="redirected").allowed
    # proposal-only resolution rejected
    bad = m.can_close(kind=mod["KIND_INQUIRY"], from_state=mod["STATE_OPEN"], resolution="accepted")
    assert not bad.allowed
    assert "not allowed for kind=inquiry" in bad.reason


def test_proposal_close_vocabulary():
    mod = _import()
    m = mod["OperationStateMachine"]()
    for ok in ["accepted", "rejected", "withdrawn", "abandoned"]:
        assert m.can_close(kind="proposal", from_state="open", resolution=ok).allowed
    assert not m.can_close(kind="proposal", from_state="open", resolution="answered").allowed


def test_task_close_vocabulary():
    mod = _import()
    m = mod["OperationStateMachine"]()
    for ok in ["completed", "failed", "cancelled", "abandoned"]:
        assert m.can_close(kind="task", from_state="executing", resolution=ok).allowed
    assert not m.can_close(kind="task", from_state="executing", resolution="accepted").allowed


def test_general_cannot_close():
    mod = _import()
    m = mod["OperationStateMachine"]()
    d = m.can_close(kind="general", from_state="open", resolution="abandoned")
    assert not d.allowed
    assert "general operations cannot be closed" in d.reason


def test_already_closed_rejects_repeat_close():
    mod = _import()
    m = mod["OperationStateMachine"]()
    d = m.can_close(kind="proposal", from_state="closed", resolution="accepted")
    assert not d.allowed
    assert "already closed" in d.reason


def test_system_bypass_accepts_abandoned_from_any_non_terminal_state():
    mod = _import()
    m = mod["OperationStateMachine"]()
    for state in ["open", "claimed", "executing", "blocked_approval", "verifying"]:
        d = m.can_close(kind="task", from_state=state, resolution="abandoned", system=True)
        assert d.allowed, state
        assert d.forced_close
    # system bypass still rejects close on already-closed
    assert not m.can_close(
        kind="task", from_state="closed", resolution="abandoned", system=True,
    ).allowed


def test_task_state_progression_open_to_claimed_to_executing():
    mod = _import()
    m = mod["OperationStateMachine"]()
    assert m.can_transition(kind="task", from_state="open", to_state="claimed").allowed
    assert m.can_transition(kind="task", from_state="claimed", to_state="executing").allowed
    assert m.can_transition(kind="task", from_state="executing", to_state="verifying").allowed


def test_task_state_invalid_transition_rejected():
    mod = _import()
    m = mod["OperationStateMachine"]()
    d = m.can_transition(kind="task", from_state="open", to_state="executing")
    assert not d.allowed
    assert "not allowed" in d.reason


def test_assert_close_raises_on_invalid():
    mod = _import()
    m = mod["OperationStateMachine"]()
    with pytest.raises(mod["StateMachineError"]):
        m.assert_close(kind="task", from_state="open", resolution="answered")


def test_assert_transition_raises_on_invalid():
    mod = _import()
    m = mod["OperationStateMachine"]()
    with pytest.raises(mod["StateMachineError"]):
        m.assert_transition(kind="task", from_state="closed", to_state="open")
