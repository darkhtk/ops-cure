"""5개 사용 사례 통합 테스트 — 프로토콜 v2 가 실제 협업 패턴에서 깨지지 않는지.

각 시나리오가 다른 feature 조합을 굴린다:

  S1 incident triage           inquiry, evidence+artifact, whisper, redaction
  S2 proposal debate           proposal vocab, two-way whisper, opener-close
  S3 task lifecycle full       state machine 풀 traverse, approval gate, artifact 누적
  S4 idle escalation           tier-1/2 warnings, tier-3 auto-abandon (system bypass)
  S5 multi-op inbox            inbox state/role filter, mark_seen cursor, multi-op 동시

매 시나리오 끝에 invariant 다발 검증. broker per-actor backlog, v2 op state,
participants, redaction, capability check 등.

In-process FastAPI TestClient + lifespan 으로 app.state.services 활성화 후
SDK BridgeV2Client 로 호출. 모든 시나리오가 v2 native write path 를 사용.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
        ChatTaskClaimRequest, ChatTaskEvidenceRequest,
        ChatTaskApprovalRequest, ChatTaskApprovalResolveRequest,
        ChatTaskCompleteRequest,
    )
    from app.behaviors.chat.models import (
        ChatThreadModel, ChatConversationModel, ChatMessageModel,
    )
    from app.behaviors.chat.task_coordinator import ChatTaskCoordinator
    from app.kernel.presence import PresenceService
    from app.kernel.approvals import KernelApprovalService
    from app.kernel.v2 import V2Repository, CapabilityService
    from app.kernel.v2 import (
        CAP_SPEECH_SUBMIT, CAP_TASK_APPROVE_DESTRUCTIVE,
        ActorService,
    )
    from app.services.remote_task_service import RemoteTaskService
    from app.agent_sdk import AgentRuntime, BridgeV2Client, IncomingEvent, BridgeV2Error
    from app.main import app
    db.init_db()
    return locals() | {"db": db}


def _make_thread(db, Thread, *, suffix: str = "1"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def _client_for(app, handle: str):
    """Wire BridgeV2Client to lifespan-active TestClient. Returns client + the
    underlying TestClient so caller can __exit__ it."""
    from app.agent_sdk import BridgeV2Client
    test_http = TestClient(app, base_url="http://testserver")
    test_http.__enter__()
    test_http.headers.update({
        "Authorization": "Bearer t",
        "X-Bridge-Client-Id": handle.lstrip("@"),
    })
    c = BridgeV2Client(
        base_url="http://testserver", bearer_token="t", actor_handle=handle,
    )
    c._http.close()
    c._http = test_http
    c._test_client = test_http
    return c


def _close(client):
    client._test_client.__exit__(None, None, None)


# =============================================================================
# S1 — Incident triage (3 actors, evidence, whisper, redaction)
# =============================================================================
def test_S1_incident_triage_with_whisper_redaction(tmp_path, monkeypatch):
    """오전 3시 production CPU spike. operator 가 inquiry 열고 worker AI 와
    reviewer AI 가 evidence 교환, worker 가 operator 에게만 사적 노트 (whisper),
    reviewer 시점에서는 그 whisper 가 가려진다. operator 가 close=answered."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _make_thread(db, m["ChatThreadModel"], suffix="incident")
    repo = m["V2Repository"]()

    operator = _client_for(m["app"], "@operator")
    pca = _client_for(m["app"], "@claude-pca")
    pcb = _client_for(m["app"], "@claude-pcb")

    try:
        # Open inquiry addressed to claude-pca
        op = operator.open_operation(
            space_id=discord, kind="inquiry",
            title="api-prod-3 CPU 99%",
            addressed_to="claude-pca",
        )
        op_id = op["id"]
        operator.append_event(
            op_id, kind="speech.question",
            text="CPU 99% started 02:55 UTC, dump 봐주세요",
            addressed_to="claude-pca",
        )

        # claude-pca: public hypothesis (using append_event directly,
        # not runtime, so we control timing exactly).
        pca.append_event(
            op_id, kind="speech.claim",
            text="py-spy: regex catastrophic backtracking.",
        )
        # ... and a private uncertainty whisper to operator only
        pca.append_event(
            op_id, kind="speech.claim",
            text="(아직 100% 확신은 없음 -- timeline 더 봐야)",
            private_to_actors=["operator"],
        )

        # operator addresses claude-pcb for review
        operator.append_event(
            op_id, kind="speech.question",
            text="@claude-pcb 다른 가능성 검토 부탁",
            addressed_to="claude-pcb",
        )

        # claude-pcb confirms
        pcb.append_event(
            op_id, kind="speech.claim",
            text="확인. q= 값에 'a*a*a*' 패턴. 단일 ReDoS 원인.",
        )

        # operator close
        closed = operator.close_operation(
            op_id, resolution="answered",
            summary="ReDoS confirmed by both reviewers",
        )
        assert closed["state"] == "closed"
        assert closed["resolution"] == "answered"

        # ---- invariants ----
        # (1) participants: 3명, 적절한 role
        op_detail = operator.get_operation(op_id)
        roles = sorted({p["role"] for p in op_detail["participants"]})
        assert "opener" in roles
        assert "addressed" in roles  # claude-pca, claude-pcb 모두 addressed

        # (2) operator 의 view: whisper 보임
        op_events = operator.list_events(op_id)
        op_texts = [e["payload"].get("text", "") for e in op_events["events"]]
        assert any("100% 확신은 없음" in t for t in op_texts)
        assert op_events["redacted_count"] == 0

        # (3) claude-pcb 의 view: whisper 가려짐
        pcb_events = pcb.list_events(op_id)
        pcb_texts = [e["payload"].get("text", "") for e in pcb_events["events"]]
        assert not any("100% 확신은 없음" in t for t in pcb_texts)
        assert pcb_events["redacted_count"] == 1

        # (4) claude-pca (speaker of whisper) 의 view: 자기 whisper 보임
        pca_events = pca.list_events(op_id)
        pca_texts = [e["payload"].get("text", "") for e in pca_events["events"]]
        assert any("100% 확신은 없음" in t for t in pca_texts)

        # (5) broker per-actor backlog: pcb 에 whisper 가 없다
        services = m["app"].state.services
        broker = services.subscription_broker
        with db.session_scope() as session:
            pcb_id = repo.get_actor_by_handle(session, "@claude-pcb").id
        pcb_backlog = list(broker._backlog.get(f"v2:inbox:{pcb_id}", []))
        pcb_backlog_text = " ".join(env.event.content for env in pcb_backlog)
        assert "100% 확신은 없음" not in pcb_backlog_text
    finally:
        _close(operator); _close(pca); _close(pcb)


# =============================================================================
# S2 — Proposal debate with two-way whisper, opener-close (withdrawn)
# =============================================================================
def test_S2_proposal_debate_withdrawn_after_private_consultation(tmp_path, monkeypatch):
    """claude-pca 가 dependency 추가 proposal 을 연다. claude-pcb 가 우려를
    공개로 표명, codex-pcc 가 claude-pcb 에게 사적으로 동의, claude-pcb 가
    claude-pca 에게 사적으로 'withdraw 권장' 한 뒤 claude-pca 가 자진 철회."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _make_thread(db, m["ChatThreadModel"], suffix="proposal")
    repo = m["V2Repository"]()

    operator = _client_for(m["app"], "@operator")
    pca = _client_for(m["app"], "@claude-pca")
    pcb = _client_for(m["app"], "@claude-pcb")
    codex = _client_for(m["app"], "@codex-pcc")

    try:
        # claude-pca opens proposal
        op = pca.open_operation(
            space_id=discord, kind="proposal",
            title="adopt re2 backend for regex engine",
            intent="ReDoS-free regex evaluation",
            addressed_to="operator",
        )
        op_id = op["id"]
        pca.append_event(
            op_id, kind="speech.propose",
            text="re2 도입 제안. ReDoS 영구 차단 + GIL 풀어줌.",
        )

        # claude-pcb 공개 우려
        pcb.append_event(
            op_id, kind="speech.object",
            text="우려: re2 의존성 추가 + Python binding 유지보수 부담.",
        )

        # codex-pcc -> claude-pcb 사적
        codex.append_event(
            op_id, kind="speech.claim",
            text="(동의. 작은 patch 로 끝낼 수 있는 문제에 큰 의존성을 끌어들이는 건 위험)",
            private_to_actors=["claude-pcb"],
        )

        # claude-pcb -> claude-pca 사적
        pcb.append_event(
            op_id, kind="speech.claim",
            text="(codex-pcc 와 사적으로 얘기했는데, withdraw 하고 작은 patch 로 가는 게 어떨까)",
            private_to_actors=["claude-pca"],
        )

        # claude-pca 자진 철회
        closed = pca.close_operation(
            op_id, resolution="withdrawn",
            summary="reviewer 사적 의견 반영, 작은 patch 로 대체 진행",
        )
        assert closed["state"] == "closed"
        assert closed["resolution"] == "withdrawn"

        # ---- invariants ----
        # (1) state machine vocab: proposal 의 'withdrawn' 통과
        # (이미 close 가 성공했으므로 통과 증명됨)

        # (2) operator 의 v2 reader: 두 whisper 모두 가려짐
        op_events = operator.list_events(op_id)
        assert op_events["redacted_count"] == 2

        # (3) claude-pcb 의 view: codex 가 보낸 whisper 보임 + 자기가 보낸 whisper 보임
        pcb_events = pcb.list_events(op_id)
        pcb_texts = [e["payload"].get("text", "") for e in pcb_events["events"]]
        assert any("작은 patch 로 끝낼" in t for t in pcb_texts)  # from codex
        assert any("withdraw 하고" in t for t in pcb_texts)  # self-sent

        # (4) claude-pcc (codex) 의 view: 자기 whisper 만 보이고
        #     claude-pcb -> claude-pca 의 whisper 는 가려짐
        codex_events = codex.list_events(op_id)
        codex_texts = [e["payload"].get("text", "") for e in codex_events["events"]]
        assert any("작은 patch 로 끝낼" in t for t in codex_texts)  # self-sent
        assert not any("withdraw 하고" in t for t in codex_texts)  # not addressee
        assert codex_events["redacted_count"] == 1

        # (5) opener authority: operator 가 close 시도하면 거부?
        from app.agent_sdk import BridgeV2Error
        # 새 proposal 열어서 operator close 시도
        op2 = pca.open_operation(
            space_id=discord, kind="proposal", title="another",
        )
        # operator 는 opener 도 owner 도 아님 -> 거부
        with pytest.raises(BridgeV2Error) as exc_info:
            operator.close_operation(op2["id"], resolution="rejected")
        assert exc_info.value.status_code in (400, 403)
    finally:
        _close(operator); _close(pca); _close(pcb); _close(codex)


# =============================================================================
# S3 — Task full lifecycle: claim → executing → blocked → executing → completed
# =============================================================================
def test_S3_task_full_lifecycle_with_approval_gate(tmp_path, monkeypatch):
    """patch ReDoS task. claude-pca 가 claim, 첫 evidence 로 executing 진입,
    destructive deploy 위해 approval 요청 → blocked_approval, operator 가
    승인 → executing 복귀, 두 번째 evidence 후 complete. v2 op.state 가
    매 단계마다 정확히 갱신되는지 검증 (#2 fix 의 실전).

    chat task 경로는 SDK 가 아닌 ChatTaskCoordinator 직접 사용 (lease token
    flow 가 native v2 endpoint 로는 아직 노출 안 됨)."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _make_thread(db, m["ChatThreadModel"], suffix="task")
    repo = m["V2Repository"]()

    # services 직접 wire (ensure_general 선행으로 SQLite 락 회피)
    remote_task = m["RemoteTaskService"](
        presence_service=m["PresenceService"](),
        kernel_approval_service=m["KernelApprovalService"](),
    )
    chat = m["ChatConversationService"](remote_task_service=remote_task)
    coord = m["ChatTaskCoordinator"](
        conversation_service=chat,
        remote_task_service=remote_task,
    )
    chat.ensure_general(discord_thread_id=discord)

    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="task", title="patch ReDoS",
            objective="fix /v1/orgs/.../audit regex; 0 prod impact",
            opener_actor="operator",
        ),
    )
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        op_id = v1.v2_operation_id

    # state=open 확인
    with db.session_scope() as s:
        assert repo.get_operation(s, op_id).state == "open"

    # claim → claimed
    claim = coord.claim(
        conversation_id=summary.id,
        request=m["ChatTaskClaimRequest"](
            actor_name="claude-pca", lease_seconds=600,
        ),
    )
    lease = claim.task["current_assignment"]["lease_token"]
    with db.session_scope() as s:
        assert repo.get_operation(s, op_id).state == "claimed"

    # 첫 evidence → executing
    coord.add_evidence(
        conversation_id=summary.id,
        request=m["ChatTaskEvidenceRequest"](
            actor_name="claude-pca", lease_token=lease,
            kind="file_write", summary="patch draft",
            payload={"artifact": {
                "kind": "patch",
                "uri": "nas://volume1/artifacts/redos-patch.diff",
                "sha256": "ab33" * 16, "mime": "text/x-diff",
                "size_bytes": 1024,
                "label": "audit.py:142 regex fix",
            }},
        ),
    )
    with db.session_scope() as s:
        assert repo.get_operation(s, op_id).state == "executing"

    # approval request → blocked_approval
    coord.request_approval(
        conversation_id=summary.id,
        request=m["ChatTaskApprovalRequest"](
            actor_name="claude-pca", lease_token=lease,
            reason="prod worker hot-restart needed",
        ),
    )
    with db.session_scope() as s:
        assert repo.get_operation(s, op_id).state == "blocked_approval"

    # operator approve → executing
    coord.resolve_approval(
        conversation_id=summary.id,
        request=m["ChatTaskApprovalResolveRequest"](
            resolved_by="operator", resolution="approved",
        ),
    )
    with db.session_scope() as s:
        assert repo.get_operation(s, op_id).state == "executing"

    # 두 번째 evidence (canary log) -- 같은 state, idempotent
    coord.add_evidence(
        conversation_id=summary.id,
        request=m["ChatTaskEvidenceRequest"](
            actor_name="claude-pca", lease_token=lease,
            kind="result", summary="canary 90s ok",
            payload={"artifact": {
                "kind": "log",
                "uri": "nas://volume1/artifacts/canary.log",
                "sha256": "cd71" * 16, "mime": "text/plain",
                "size_bytes": 4096,
            }},
        ),
    )

    # complete → closed/completed
    coord.complete(
        conversation_id=summary.id,
        request=m["ChatTaskCompleteRequest"](
            actor_name="claude-pca", lease_token=lease,
            summary="ReDoS patched, p99 회복",
        ),
    )
    with db.session_scope() as s:
        op = repo.get_operation(s, op_id)
        assert op.state == "closed"
        assert op.resolution == "completed"

    # ---- invariants ----
    # (1) artifacts: 2 evidence 첨부 (log, patch) + close 시 digest 가
    #     붙인 summary card 1개 = 총 3개. summary 는 close 직후
    #     자동 부여됨 (behaviors/digest).
    with db.session_scope() as s:
        artifacts = repo.list_artifacts_for_operation(s, operation_id=op_id)
        kinds = sorted(a.kind for a in artifacts)
        assert kinds == ["log", "patch", "summary"]

    # (2) event sequence: opened, claim, evidence, approval.req, approval.resolve,
    #     evidence, complete, closed
    with db.session_scope() as s:
        events = repo.list_events(s, operation_id=op_id, limit=100)
        kinds = [e.kind for e in events]
        assert kinds[0] == "chat.conversation.opened"
        assert kinds[-1] == "chat.conversation.closed"
        assert "chat.task.claimed" in kinds
        assert kinds.count("chat.task.evidence") == 2
        assert "chat.task.approval_requested" in kinds
        assert "chat.task.approval_resolved" in kinds
        assert "chat.task.completed" in kinds


# =============================================================================
# S4 — Idle escalation: tier-1, tier-2, tier-3 auto-abandon
# =============================================================================
def test_S4_idle_escalation_auto_abandons(tmp_path, monkeypatch):
    """operator 가 inquiry 열고 답변 없는 상태로 시간이 흐른다. sweep_idle
    이 tier-1/2 warning 을 emit 하고, tier-3 임계 도달 시 system bypass 로
    'abandoned' 자동 close. v2 state machine 의 system=True 경로 검증."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _make_thread(db, m["ChatThreadModel"], suffix="idle")
    repo = m["V2Repository"]()

    # 정책: tier 1=1x, 2=2x, 3=3x base. 작은 multiplier 로 단축.
    from app.behaviors.chat.conversation_service import ChatPolicyConfig
    policy = ChatPolicyConfig(
        tier_1_multiplier=1, tier_2_multiplier=2, tier_3_multiplier=3,
    )
    svc = m["ChatConversationService"](policy=policy)

    # general 미리 만들어 lock 회피
    svc.ensure_general(discord_thread_id=discord)

    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="quiet question", opener_actor="operator",
        ),
    )

    # last_speech_at 을 충분히 과거로 backdate (tier-3 임계 초과)
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=30)
    with db.session_scope() as s:
        row = s.get(m["ChatConversationModel"], summary.id)
        row.last_speech_at = long_ago
        row.created_at = long_ago

    # threshold=1초, multiplier (1,2,3) -> tier 임계 1/2/3 초.
    # age 가 30초이므로 tier-3 도달 -> auto-abandon.
    flagged = svc.sweep_idle_conversations(
        discord_thread_id=discord,
        idle_threshold_seconds=1,
    )

    # tier-3 라 flagged 에는 안 들고 (auto-close 됐음) state 만 확인
    with db.session_scope() as s:
        v1 = s.get(m["ChatConversationModel"], summary.id)
        assert v1.state == "closed"
        assert v1.resolution == "abandoned"
        assert v1.idle_warning_count == 3

        # v2 op 도 closed/abandoned
        op = repo.get_operation(s, v1.v2_operation_id)
        assert op.state == "closed"
        assert op.resolution == "abandoned"

        # event log 에 idle_warning level=1, level=2 + closed 모두 존재
        events = repo.list_events(s, operation_id=v1.v2_operation_id, limit=100)
        kinds = [e.kind for e in events]
        assert kinds.count("chat.conversation.idle_warning") == 2
        assert "chat.conversation.closed" in kinds

        # idle_warning event 의 lifecycle 에 level=1, level=2 있음
        warning_evs = [e for e in events if e.kind == "chat.conversation.idle_warning"]
        levels = sorted(repo.event_payload(e)["lifecycle"]["level"] for e in warning_evs)
        assert levels == [1, 2]


# =============================================================================
# S5 — Multi-op operator inbox (filter, cursor, mark_seen)
# =============================================================================
def test_S5_operator_running_three_ops_concurrently(tmp_path, monkeypatch):
    """operator 가 동시에 3개 op 을 돌린다. inbox 가 모두 보여주고, state/role
    필터가 정확히 자르고, mark_seen 이 unread_count 를 줄인다. 한 op 닫으면
    state=open 결과에서 빠진다."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _make_thread(db, m["ChatThreadModel"], suffix="multi")
    repo = m["V2Repository"]()

    operator = _client_for(m["app"], "@operator")
    pca = _client_for(m["app"], "@claude-pca")

    try:
        # op-A: operator 가 연 inquiry, claude-pca 에게 addressed
        op_a = operator.open_operation(
            space_id=discord, kind="inquiry",
            title="op-A inquiry", addressed_to="claude-pca",
        )["id"]
        # op-B: operator 가 연 proposal (addressee 없음)
        op_b = operator.open_operation(
            space_id=discord, kind="proposal", title="op-B proposal",
        )["id"]
        # op-C: claude-pca 가 연 inquiry, operator 에게 addressed
        op_c = pca.open_operation(
            space_id=discord, kind="inquiry",
            title="op-C inquiry", addressed_to="operator",
        )["id"]

        # operator 의 inbox 3개 모두
        inbox_all = operator.get_inbox()
        all_ids = {item["operation_id"] for item in inbox_all["items"]}
        assert {op_a, op_b, op_c}.issubset(all_ids)

        # state=open 도 3개 (모두 미체결)
        inbox_open = operator.get_inbox(state="open")
        assert len({i["operation_id"] for i in inbox_open["items"]}) >= 3

        # role=opener: A, B (operator 가 연 것)
        inbox_opener = operator.get_inbox(roles=["opener"])
        opener_ids = {i["operation_id"] for i in inbox_opener["items"]}
        assert opener_ids.issuperset({op_a, op_b})
        assert op_c not in opener_ids

        # role=addressed: C (claude-pca 가 operator 에게 보낸 것)
        inbox_addr = operator.get_inbox(roles=["addressed"])
        addr_ids = {i["operation_id"] for i in inbox_addr["items"]}
        assert op_c in addr_ids
        assert op_a not in addr_ids
        assert op_b not in addr_ids

        # operator unread_count: 일단 0 보다 큼
        unread_before = operator.get_unread_count()
        assert unread_before > 0

        # op-A 의 가장 큰 seq 까지 mark_seen
        a_events = operator.list_events(op_a)
        a_last_seq = a_events["events"][-1]["seq"]
        operator.mark_seen(op_a, seq=a_last_seq)

        # unread 가 줄었는지 (op-A 의 events 만큼)
        unread_after = operator.get_unread_count()
        assert unread_after < unread_before

        # op-A close
        operator.close_operation(op_a, resolution="answered")

        # state=open 에서 op-A 빠짐
        inbox_open_2 = operator.get_inbox(state="open")
        open_ids_2 = {i["operation_id"] for i in inbox_open_2["items"]}
        assert op_a not in open_ids_2
        assert {op_b, op_c}.issubset(open_ids_2)

        # state=closed 에서 op-A 만
        inbox_closed = operator.get_inbox(state="closed")
        closed_ids = {i["operation_id"] for i in inbox_closed["items"]}
        assert op_a in closed_ids
    finally:
        _close(operator); _close(pca)
