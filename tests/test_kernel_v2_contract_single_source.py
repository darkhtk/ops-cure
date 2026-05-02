"""γ: Protocol Contract 가 단일 소스인지, 모든 소비자가 같은 dict 객체
를 보는지, drift 가 들어오면 module load 단계에서 잡히는지 검증."""
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
    from app.kernel.v2 import state_machine, capabilities
    from app.behaviors.chat import conversation_schemas as v1_schemas
    return locals()


def test_state_machine_resolutions_are_the_contract_object():
    """Identity check: state_machine.ALLOWED_RESOLUTIONS IS the
    same object as contract.ALLOWED_RESOLUTIONS. Anyone who patches
    one sees the other."""
    m = _import()
    assert m["state_machine"].ALLOWED_RESOLUTIONS is m["contract"].ALLOWED_RESOLUTIONS
    assert m["state_machine"].ALLOWED_TRANSITIONS is m["contract"].ALLOWED_TRANSITIONS


def test_v1_schema_resolutions_are_the_contract_object():
    """Same for v1 layer: ALLOWED_RESOLUTIONS_BY_KIND IS the
    contract dict. The pre-γ duplication where v1 had its own
    smaller set is gone."""
    m = _import()
    assert m["v1_schemas"].ALLOWED_RESOLUTIONS_BY_KIND is m["contract"].ALLOWED_RESOLUTIONS


def test_capabilities_default_sets_are_the_contract_object():
    m = _import()
    assert m["capabilities"].ALL_DEFAULT_HUMAN is m["contract"].DEFAULT_CAPABILITIES_HUMAN
    assert m["capabilities"].ALL_DEFAULT_AI is m["contract"].DEFAULT_CAPABILITIES_AI


def test_speech_kind_drift_is_caught_at_load():
    """Simulating drift: if conversation_schemas.SpeechKind disagreed
    with contract.SPEECH_KINDS, module import would have raised. We
    re-call the asserter with a fake mismatched Literal to prove the
    detector itself works."""
    m = _import()
    from typing import Literal
    fake_literal = Literal["claim", "question", "made_up_kind"]
    with pytest.raises(AssertionError) as exc:
        m["v1_schemas"]._assert_literal_matches_contract(
            fake_literal, m["contract"].SPEECH_KINDS, "FakeSpeechKind",
        )
    assert "FakeSpeechKind drift" in str(exc.value)
    # The error message points at exactly what's missing.
    assert "made_up_kind" in str(exc.value) or "only-in-schema" in str(exc.value)


def test_evidence_kind_drift_caught_with_missing_value_in_schema():
    m = _import()
    from typing import Literal
    # schema misses 'screenshot' that contract has
    fake_literal = Literal["command_execution", "result"]
    with pytest.raises(AssertionError) as exc:
        m["v1_schemas"]._assert_literal_matches_contract(
            fake_literal, m["contract"].EVIDENCE_KINDS, "FakeEvidenceKind",
        )
    msg = str(exc.value)
    assert "FakeEvidenceKind drift" in msg
    # contract-only items appear in the message
    assert "only-in-contract" in msg


def test_contract_self_consistency_runs_at_import():
    """validate_contract was already called on import; here we
    re-run it to confirm it still passes for the live contract."""
    m = _import()
    m["contract"].validate_contract()  # must not raise


def test_event_kind_to_target_state_table_is_internally_consistent():
    """Every entry in EVENT_KIND_TO_TARGET_STATE points at a state
    that some kind can actually reach."""
    m = _import()
    contract = m["contract"]
    all_targets = set()
    for graph in contract.ALLOWED_TRANSITIONS.values():
        for tgts in graph.values():
            all_targets.update(tgts)
    for ev_kind, target_state in contract.EVENT_KIND_TO_TARGET_STATE.items():
        assert target_state in all_targets, (
            f"event {ev_kind} points to unreachable state {target_state}"
        )


def test_close_resolution_uses_contract_vocab_through_v1():
    """v1 is_resolution_allowed reads through the contract; adding a
    resolution to contract immediately makes v1 accept it."""
    m = _import()
    is_ok = m["v1_schemas"].is_resolution_allowed
    # 'abandoned' is in contract.ALLOWED_RESOLUTIONS for inquiry
    assert is_ok(kind="inquiry", resolution="abandoned")
    # 'completed' is task vocab, NOT inquiry
    assert not is_ok(kind="inquiry", resolution="completed")


def test_task_coordinator_uses_contract_for_state_transitions(tmp_path, monkeypatch):
    """End-to-end: ChatTaskCoordinator no longer hardcodes new_v2_state
    at 3 of 4 sites. The contract.EVENT_KIND_TO_TARGET_STATE table
    drives the transition. Patching the contract for one event_kind
    flips the behavior live."""
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, ChatTaskClaimRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.behaviors.chat.task_coordinator import ChatTaskCoordinator
    from app.kernel.presence import PresenceService
    from app.kernel.approvals import KernelApprovalService
    from app.kernel.v2 import V2Repository, contract as v2_contract
    from app.services.remote_task_service import RemoteTaskService
    db.init_db()

    # Patch contract: route chat.task.claimed to 'verifying' instead.
    # This shouldn't be possible logically (state machine bars open->verifying)
    # so we expect the assert_transition to RAISE -- proving the lookup
    # path is the one driving the transition.
    import uuid
    with db.session_scope() as s:
        t = ChatThreadModel(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        discord = t.discord_thread_id

    remote_task = RemoteTaskService(
        presence_service=PresenceService(),
        kernel_approval_service=KernelApprovalService(),
    )
    chat = ChatConversationService(remote_task_service=remote_task)
    coord = ChatTaskCoordinator(
        conversation_service=chat,
        remote_task_service=remote_task,
    )
    chat.ensure_general(discord_thread_id=discord)
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=ConversationOpenRequest(
            kind="task", title="t",
            objective="do", opener_actor="alice",
        ),
    )

    monkeypatch.setitem(
        v2_contract.EVENT_KIND_TO_TARGET_STATE,
        "chat.task.claimed", "verifying",
    )
    from app.kernel.v2 import StateMachineError
    with pytest.raises(StateMachineError):
        coord.claim(
            conversation_id=summary.id,
            request=ChatTaskClaimRequest(
                actor_name="claude-pca", lease_seconds=300,
            ),
        )
