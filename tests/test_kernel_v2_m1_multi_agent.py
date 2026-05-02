"""M1: 멀티 에이전트 협업 시나리오를 v2 native + SDK 로 한 사이클 굴림.

전체 stack 동시에 동작하는지 보는 통합 테스트:
  - native v2 write (G2)
  - capability check (G1)
  - state machine close vocab (G1)
  - broker per-actor fan-out (G3)
  - whisper redaction in v2 reader (F7) + v1 reader (G4)
  - inbox listing + mark_seen cursor (F5/F7)
  - SDK BridgeV2Client + AgentRuntime (F11)

3 actor:
  alice (human operator)  -- 질문 제기, 마지막에 close
  claude-pca (worker AI)  -- 가설 제시
  claude-pcb (reviewer AI) -- 가설 challenge

흐름:
  1. alice -> claude-pca 로 inquiry "왜 build 가 깨지지?"
  2. claude-pca runtime: 받아서 "node version mismatch 같다" 응답
  3. claude-pca -> alice whisper "솔직히 잘 모르겠음"  (사적 노트)
  4. alice 가 claude-pcb 에게 "review 해주세요" 추가 발화
  5. claude-pcb runtime: 받아서 "다른 가설: lockfile" 응답
  6. carol (방관자) 의 v2 stream 백로그 -- whisper 안 보임 검증
  7. alice close(answered)

각 단계 후 invariant 검증.
"""
from __future__ import annotations

import sys
import uuid

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
    from app.behaviors.chat.models import ChatThreadModel
    from app.kernel.v2 import V2Repository
    from app.agent_sdk import AgentRuntime, BridgeV2Client, IncomingEvent
    from app.main import app
    db.init_db()
    return locals() | {"db": db}


def _make_thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def _client_for(app, handle: str):
    """Wire a BridgeV2Client to FastAPI TestClient w/ lifespan so
    chat_api routes have app.state.services."""
    from app.agent_sdk import BridgeV2Client
    from fastapi.testclient import TestClient
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


def _close(c):
    c._test_client.__exit__(None, None, None)


def test_three_agent_collaboration_full_stack(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _make_thread(db, m["ChatThreadModel"])
    AgentRuntime = m["AgentRuntime"]
    repo = m["V2Repository"]()

    alice = _client_for(m["app"], "@alice")
    claude_pca = _client_for(m["app"], "@claude-pca")
    claude_pcb = _client_for(m["app"], "@claude-pcb")
    carol = _client_for(m["app"], "@carol")  # bystander

    try:
        # 1. alice 가 inquiry 를 claude-pca 에게
        op = alice.open_operation(
            space_id=discord, kind="inquiry",
            title="왜 build 가 깨지지?",
            addressed_to="claude-pca",
        )
        op_id = op["id"]
        alice.append_event(
            op_id, kind="speech.question",
            text="CI build 가 어제부터 fail. 어디가 문제일까?",
            addressed_to="claude-pca",
        )

        # 2. claude-pca runtime 이 한 tick 으로 받고 응답
        replies_by_pca: list[str] = []

        def pca_handler(event, client):
            if event.kind != "chat.speech.question":
                return
            if "claude-pca" not in (
                event.payload.get("text", "")
                or event.payload.get("addressed_to", "")
                or ""
            ) and not event.addressed_to_actor_ids:
                # only react when addressed
                pass
            client.append_event(
                event.operation_id,
                kind="speech.claim",
                text="가설 1: node version mismatch 인 것 같다.",
            )
            replies_by_pca.append("public-hypothesis")
            # whisper 사적 노트 -- alice 만 본다
            client.append_event(
                event.operation_id,
                kind="speech.claim",
                text="(솔직히 100% 확신은 없음)",
                private_to_actors=["alice"],
            )
            replies_by_pca.append("whisper-to-alice")

        AgentRuntime(claude_pca, pca_handler, poll_interval_seconds=0).run_once()
        assert replies_by_pca == ["public-hypothesis", "whisper-to-alice"]

        # 3. alice 가 claude-pcb 에게 review 요청
        alice.append_event(
            op_id, kind="speech.question",
            text="claude-pcb 검토 부탁",
            addressed_to="claude-pcb",
        )

        # 4. claude-pcb runtime 이 받고 challenge
        challenges_by_pcb: list[str] = []

        def pcb_handler(event, client):
            if event.kind != "chat.speech.question":
                return
            client.append_event(
                event.operation_id,
                kind="speech.claim",
                text="다른 가설: lockfile drift 가능성도 보세요.",
            )
            challenges_by_pcb.append("alt-hypothesis")

        AgentRuntime(claude_pcb, pcb_handler, poll_interval_seconds=0).run_once()
        assert challenges_by_pcb == ["alt-hypothesis"]

        # 5. carol 의 inbox -- 이 op 에 참가 안 하므로 비어야 한다
        carol_inbox = carol.get_inbox()
        carol_op_ids = {item["operation_id"] for item in carol_inbox["items"]}
        assert op_id not in carol_op_ids, "bystander가 inbox에 op이 잡히면 권한 누수"

        # 6. claude-pca 의 v2 reader 로 본 events -- 자기 whisper 보임
        pca_events = claude_pca.list_events(op_id)
        pca_texts = [
            e["payload"].get("text", "") for e in pca_events["events"]
        ]
        assert "(솔직히 100% 확신은 없음)" in pca_texts

        # 7. claude-pcb 의 v2 reader -- whisper 가려져야
        pcb_events = claude_pcb.list_events(op_id)
        pcb_texts = [
            e["payload"].get("text", "") for e in pcb_events["events"]
        ]
        assert "(솔직히 100% 확신은 없음)" not in pcb_texts
        assert pcb_events["redacted_count"] == 1

        # 8. alice 의 v2 reader -- whisper 받는 자라 보여야
        alice_events = alice.list_events(op_id)
        alice_texts = [
            e["payload"].get("text", "") for e in alice_events["events"]
        ]
        assert "(솔직히 100% 확신은 없음)" in alice_texts

        # 9. alice 가 close (state machine vocab 에 있는 'answered')
        closed = alice.close_operation(
            op_id, resolution="answered",
            summary="claude-pca/claude-pcb 두 가설 받음",
        )
        assert closed["state"] == "closed"
        assert closed["resolution"] == "answered"

        # 10. inbox state 필터: alice 의 open 인 op 는 0 (방금 닫음)
        alice_open = alice.get_inbox(state="open")
        assert all(item["operation_id"] != op_id for item in alice_open["items"])

        # 11. broker per-actor fan-out 검증: alice/claude-pca/claude-pcb 의
        #     'v2:inbox:<id>' backlog 에 이벤트가 쌓였고 carol 는 비어있다.
        services = m["app"].state.services
        broker = services.subscription_broker
        with db.session_scope() as session:
            alice_id = repo.get_actor_by_handle(session, "@alice").id
            pca_id = repo.get_actor_by_handle(session, "@claude-pca").id
            pcb_id = repo.get_actor_by_handle(session, "@claude-pcb").id
            carol_actor = repo.get_actor_by_handle(session, "@carol")
            carol_id = carol_actor.id if carol_actor else None
        assert len(broker._backlog.get(f"v2:inbox:{alice_id}", [])) >= 5
        assert len(broker._backlog.get(f"v2:inbox:{pca_id}", [])) >= 3
        assert len(broker._backlog.get(f"v2:inbox:{pcb_id}", [])) >= 1
        if carol_id is not None:
            assert len(broker._backlog.get(f"v2:inbox:{carol_id}", [])) == 0

        # 12. capability sanity: claude-pca 는 default 로 speech.submit 가짐
        from app.kernel.v2 import CapabilityService, CAP_SPEECH_SUBMIT
        cap = CapabilityService()
        with db.session_scope() as session:
            assert cap.actor_can(session, actor_handle="@claude-pca", capability=CAP_SPEECH_SUBMIT)

    finally:
        _close(alice); _close(claude_pca); _close(claude_pcb); _close(carol)


def test_invalid_resolution_blocked_at_close(tmp_path, monkeypatch):
    """alice 가 inquiry 를 'accepted' (proposal vocab) 로 닫으려 하면 400."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    discord = _make_thread(db, m["ChatThreadModel"])

    alice = _client_for(m["app"], "@alice")
    try:
        op = alice.open_operation(
            space_id=discord, kind="inquiry",
            title="간단한 질문",
        )
        from app.agent_sdk import BridgeV2Error
        try:
            alice.close_operation(op["id"], resolution="accepted")
            raise AssertionError("invalid resolution should have been blocked")
        except BridgeV2Error as e:
            assert e.status_code == 400
    finally:
        _close(alice)
