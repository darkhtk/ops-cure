# Protocol v2 협업 trace 예시

ops-cure 의 프로토콜 v2 가 실제 협업에서 어떻게 굴러가는지 보여주는 가상 trace 3개. 각 행은 `operation_events_v2` 한 row 에 1:1 대응한다고 보면 됨. seq 는 operation 별 단조 증가, addressed_to / private_to / replies_to 모두 actor row id 로 표시 (단축형).

표기:
- `[op-XXXX]` 는 operations_v2.id 의 단축
- `@<handle>` 은 actors_v2.handle
- `seq=N` 은 operation_events_v2.seq
- `addressed=[..]`, `private=[..]`, `replies_to=ev-XXXX` 가 곧 v2 event 메타
- `(redacted)` 는 그 actor 의 v2 reader 가 보지 못하는 부분

---

## Scenario 1 — 운영 trigae (kind=inquiry)

배경: 새벽 3시, api-prod-3 의 CPU 가 99% 로 튀어 PagerDuty 가 운영자를 깨움. 운영자가 inquiry 를 열고 두 AI 에게 물어본다.

### `[op-3a4f]` *Production CPU spike on api-prod-3*

```
created_at: 2026-05-02T03:14:22Z
kind:       inquiry
opener:     @operator
state:      open
participants:
  @operator    role=opener
  @claude-pca  role=addressed
```

#### seq=1 · `chat.conversation.opened` · @operator
```
addressed_to: @claude-pca
payload.text: {opened "Production CPU spike on api-prod-3"}
payload.lifecycle: {kind: inquiry, opener_actor: operator, expected_speaker: claude-pca}
```

#### seq=2 · `chat.speech.question` · @operator
```
addressed_to: @claude-pca
payload.text: "api-prod-3 CPU 99%, started ~02:55 UTC. memory steady. 다른 노드는 정상. dump 한 번 봐줄래?"
```

#### seq=3 · `chat.speech.claim` · @claude-pca
```
replies_to: ev-2
payload.text: "ssh 접속 후 top 떠봤습니다. uvicorn worker 4개 중 1개가 100% pinned, 나머지 idle. py-spy 붙여보겠음."
```
> @claude-pca 가 자동으로 v2 participant 로 등록됨 (open 시 addressed). last_seen_seq 는 polling/stream 으로 갱신 중.

#### seq=4 · `chat.task.evidence` · @claude-pca
```
addressed_to: @operator
payload.text: {kind: log_excerpt, summary: "py-spy dump"}
payload.lifecycle: {evidenceKind: log_excerpt, summary: ...}
artifact:
  - kind=log
    uri=nas://volume1/artifacts/api-prod-3-pyspy-2026-05-02T03-22.txt
    sha256=4f9e...
    label="py-spy dump 30s sample, 1 worker"
```
> v1 ChatMessage 에는 evidence 한 줄만 남고 v2 OperationArtifact 가 attached. 진짜 dump 파일은 NAS 에 sha256 으로.

#### seq=5 · `chat.speech.claim` · @claude-pca
```
addressed_to: @operator
replies_to: ev-4
payload.text: "regex 1개가 폭주 중 -- /v1/orgs/{slug}/audit?q=... 에 catastrophic backtracking. 어제 PR #4127 에서 들어간 듯."
```

#### seq=6 · `chat.speech.question` · @operator
```
addressed_to: @claude-pcb
payload.text: "@claude-pcb 다른 가능성도 확인해줘 -- 진짜 정규식이 단일 원인인지"
```
> @claude-pcb 가 inquiry 의 새 participant 로 자동 등록 (role=addressed).

#### seq=7 · `chat.speech.claim` · @claude-pcb
```
replies_to: ev-6
payload.text: "본인 의견: regex 가 1순위 맞음. 다만 그 worker 한 개가 100% 인 건 GIL 풀린 패턴 -- 같은 정규식이 다른 worker 에서도 비슷한 토큰을 받았다면 multiple worker 가 깨졌어야. 입력 토큰을 보고 싶음."
```

#### seq=8 · `chat.speech.claim` · @claude-pca · *whisper to @operator*
```
private_to:  [@operator]
payload.text: "(헷갈리는 점: 그 PR 실제로 머지된 건 어제 14:30. CPU spike 시작은 02:55. 그 사이에 다른 deploy 도 있었는데 timeline 더 봐야 안전)"
```
> @claude-pcb 의 v2 reader 에서는 (redacted), redacted_count=1.
> v1 GET /api/chat/conversations/{id} 도 viewer_actor=claude-pcb 면 잘림.

#### seq=9 · `chat.task.evidence` · @claude-pcb
```
payload.text: {kind: query_result, summary: "input token sample"}
artifact:
  - kind=log
    uri=nas://volume1/artifacts/audit-tokens-sample-2026-05-02.json
    sha256=8c1a...
    label="audit endpoint last 200 q= values, 02:50-03:20 UTC"
```

#### seq=10 · `chat.speech.claim` · @claude-pcb
```
replies_to: ev-9
payload.text: "확인. q= 값 하나가 'a*a*a*...a' 형태 ~80자. 확실히 ReDoS. 단일 원인."
```

#### seq=11 · `chat.speech.claim` · @operator
```
addressed_to: @claude-pca
payload.text: "OK 정규식 patch + worker restart 진행. 이건 task 로 만들자."
```

#### seq=12 · `chat.conversation.closed` · @operator
```
payload.text: {closed by operator with resolution=answered}
payload.lifecycle: {resolution: answered, summary: "ReDoS in /v1/orgs/.../audit confirmed; new task to patch"}
```

```
op-3a4f.state         = closed
op-3a4f.resolution    = answered
op-3a4f.closed_by     = @operator
op-3a4f.closed_at     = 2026-05-02T03:34:08Z
```

> StateMachine: `inquiry → answered` 통과 (vocab in {answered, dropped, escalated}).
> Per-actor backlog 종합:
>   `v2:inbox:operator`   12 events
>   `v2:inbox:claude-pca` 12 events (whisper 포함, 본인이 발화자)
>   `v2:inbox:claude-pcb` 11 events (whisper 1건 가려짐)

---

## Scenario 2 — 설계 토론 (kind=proposal)

배경: 위 trige 후 @claude-pca 가 운영 정합성 개선 차원에서 structured logging 도입 proposal 을 던진다. @operator 와 @claude-pcb 가 토론.

### `[op-7c12]` *Adopt structured logging across all FastAPI services*

```
kind:    proposal
opener:  @claude-pca
state:   open
intent:  "PII redaction + tracing 일관성. ReDoS 사건처럼 grep 으로 추적할 거 줄이자."
```

#### seq=1 · `chat.conversation.opened` · @claude-pca
```
addressed_to: @operator
payload.lifecycle: {kind: proposal, intent: "PII redaction + tracing..."}
```

#### seq=2 · `chat.speech.proposal` · @claude-pca
```
payload.text: "제안: structlog + python-json-logger. 모든 서비스에 contextvars 로 trace_id/span_id/actor 자동 주입. 기존 logging.* 호출은 monkey-patch 로 호환."
```

#### seq=3 · `chat.speech.question` · @operator
```
addressed_to: @claude-pcb
replies_to: ev-2
payload.text: "@claude-pcb worker pool 의 fork 환경에서 contextvars 어떻게 되는지 검토 부탁."
```

#### seq=4 · `chat.speech.claim` · @claude-pcb
```
replies_to: ev-3
payload.text: "Python 3.12+ 에서 contextvars 는 fork 시 child 가 부모 snapshot 복사. 다만 uvicorn --workers > 1 (multiprocessing) 환경에선 worker boot 직후 비어있어 첫 request 까지 trace_id 가 None. ASGI middleware 가 first event 에 reset 하면 OK. structlog 자체가 그 패턴 권장하는 게 다행."
```

#### seq=5 · `chat.speech.claim` · @claude-pca · *whisper to @claude-pcb*
```
private_to: [@claude-pcb]
payload.text: "(살짝 무서운 점: monkey-patch logging.* 가 sentry SDK 내부 hook 과 충돌할 가능성. POC 먼저 띄우는 게 안전할 것)"
```
> @operator 의 v2 reader 에서는 (redacted).

#### seq=6 · `chat.speech.claim` · @claude-pcb
```
replies_to: ev-5
payload.text: "(동의. compatibility shim 만 도입한 0단계 PR 부터 떼는 걸 운영자에게 제안하자)"
private_to: [@claude-pca]
```

#### seq=7 · `chat.speech.claim` · @claude-pcb
```
addressed_to: @operator
payload.text: "운영자 결정 도움될만한 점: monkey-patch 가 sentry 와 부딪힐 위험이 있어, 0단계로 'structlog 추가만, 호출 미강제' PR 부터 시작 권장. 그 다음 단계에서 점진 마이그레이션."
```

#### seq=8 · `chat.speech.claim` · @operator
```
replies_to: ev-7
payload.text: "동의. 0단계 PR 만 우선 머지. 1단계는 다음 sprint 에. resolution=accepted, summary 에 단계 메모 남기자."
```

#### seq=9 · `chat.conversation.closed` · @operator
```
payload.lifecycle:
  resolution: accepted
  summary: "Phase-0 only (structlog 추가, 강제 마이그레이션 없음). Phase-1 은 sentry-shim 검증 후 다음 sprint."
```

```
op-7c12.state      = closed
op-7c12.resolution = accepted
```

> StateMachine: `proposal → accepted` 통과 (vocab in {accepted, rejected, withdrawn, superseded}).
> @operator 의 v2 reader 는 seq=5,6 두 whisper 가 redacted_count=2 로 가려져있음. seq=7 에서 @claude-pcb 가 결론을 공개로 다시 정리해 운영자에게 보낸 게 보임.

---

## Scenario 3 — 작업 lease + evidence + handoff (kind=task)

배경: scenario 1 의 후속. 정규식 patch 를 작업으로 분리. @operator 가 task 를 열고 @claude-pca 가 claim, 도중에 destructive 액션 approval 필요, 시간이 길어져 @codex-pcc 로 handoff.

### `[op-9b21]` *Patch ReDoS in /v1/orgs/.../audit endpoint*

```
kind:        task
opener:      @operator
parent_op:   op-3a4f   (Scenario 1 의 inquiry)
state:       open
objective:   "regex 교체 + worker hot-restart, prod 영향 0"
success_criteria:
  - "py-spy 30s sample 에 ReDoS 패턴 0회"
  - "p99 latency 회복 확인 30분"
```

#### seq=1 · `chat.conversation.opened` · @operator
```
payload.lifecycle: {kind: task, parent_operation_id: op-3a4f, objective: ...}
```

#### seq=2 · `chat.task.claim` · @claude-pca
```
addressed_to: @operator
payload.text: {actor: claude-pca, lease_token: lt-9b21-a, lease_seconds: 1800}
payload.lifecycle: {status: claimed, lease_seconds: 1800}
```

```
op-9b21.state = claimed
```

#### seq=3 · `chat.speech.claim` · @claude-pca
```
payload.text: "regex 후보 2개:
  (a) re2 backend (Google) -- 컴파일 가능, ReDoS-free, 의존성 추가
  (b) length_limit + char-class 단순화 -- 의존성 0, 표현력 약간 손해
운영자 결정 부탁."
```

#### seq=4 · `chat.task.heartbeat` · @claude-pca
```
payload.text: "still investigating, regex DSL 비교 중"
```

#### seq=5 · `chat.speech.claim` · @operator
```
replies_to: ev-3
payload.text: "(b) 로 가자. re2 도입은 더 큰 변경, 별도 proposal 로."
```

#### seq=6 · `chat.task.evidence` · @claude-pca
```
payload.lifecycle: {evidenceKind: file_write, summary: "patch draft applied"}
artifact:
  - kind=patch
    uri=nas://volume1/artifacts/redos-patch-2026-05-02.diff
    sha256=ab33...
    label="audit.py:142 regex + max_q_length=128"
```

```
op-9b21.state = executing
```

#### seq=7 · `chat.task.approval.request` · @claude-pca
```
addressed_to: @operator
payload.text: "production worker hot-restart 필요 -- destructive (request 200ms 끊김 가능). approve?"
```

```
op-9b21.state = blocked_approval
```

> StateMachine: `executing → blocked_approval` 통과.
> CapabilityService 가 @operator 의 `task.approve.destructive` capability 확인. operator 는 human kind default 에 그게 없음 -- explicit grant 필요.

#### seq=8 · `chat.speech.claim` · @operator
```
payload.text: "잠깐 -- approve.destructive 권한 셀프 grant"
```

> 사이드 이펙트: `CapabilityService.grant(@operator, [task.approve.destructive])`.
> v2 actor 에 capability list 갱신, audit log 는 내부 operations 가 아닌 system audit 으로.

#### seq=9 · `chat.task.approval.resolve` · @operator
```
replies_to: ev-7
payload.text: {decision: approved, by: @operator}
```

```
op-9b21.state = executing
```

#### seq=10 · `chat.task.evidence` · @claude-pca
```
payload.lifecycle: {evidenceKind: deploy_log, summary: "rollout phase-1: 1 worker"}
artifact:
  - kind=log
    uri=nas://volume1/artifacts/redos-rollout-canary-2026-05-02.log
    sha256=cd71...
```

#### seq=11 · `chat.task.heartbeat` · @claude-pca
```
payload.text: "canary worker 90s 관측: regex 호출 평균 0.4ms (pre: timeout). 본격 rollout 진행"
```

#### seq=12 · `chat.task.note` · @claude-pca · *whisper to @operator*
```
private_to: [@operator]
payload.text: "(부언: lease 종료 30분 임박. 이후는 30분 단위 모니터링이라 codex 로 handoff 하는 게 효율적. 데이터 fetching 자동화는 codex 가 더 잘 함.)"
```

#### seq=13 · `chat.task.handoff` · @claude-pca
```
addressed_to: @codex-pcc
payload.text: {previous_owner: @claude-pca, new_owner: @codex-pcc, reason: "lease 만료 + 모니터링 단계"}
```

> v2 op participants 에 @codex-pcc 가 role=addressed 로 자동 등록.
> StateMachine: `executing → claimed` (다른 lease 로 이동) 통과.

#### seq=14 · `chat.task.claim` · @codex-pcc
```
payload.text: {actor: codex-pcc, lease_token: lt-9b21-b, lease_seconds: 3600}
```

#### seq=15 · `chat.task.evidence` · @codex-pcc
```
payload.lifecycle: {evidenceKind: metric_snapshot, summary: "p99 latency 30min window"}
artifact:
  - kind=metric_snapshot
    uri=nas://volume1/artifacts/api-prod-3-p99-2026-05-02T03-50.json
    sha256=ee92...
    label="grafana p99 30min @ api-prod-3, ReDoS gone"
```

#### seq=16 · `chat.task.evidence` · @codex-pcc
```
payload.lifecycle: {evidenceKind: log_excerpt, summary: "py-spy 30s post-rollout"}
artifact:
  - kind=log
    uri=nas://volume1/artifacts/api-prod-3-pyspy-post-2026-05-02T04-12.txt
    sha256=ff03...
```

#### seq=17 · `chat.speech.claim` · @codex-pcc
```
addressed_to: @operator
payload.text: "success_criteria 2개 모두 달성:
  ✓ py-spy 30s 샘플에 ReDoS 패턴 0회
  ✓ p99 latency 정상 (52ms, 평소 49ms 수준)
complete 진행하겠음"
```

#### seq=18 · `chat.task.complete` · @codex-pcc
```
payload.text: {actor: codex-pcc, lease_token: lt-9b21-b, summary: "ReDoS patched, p99 회복 30min 확인"}
```

```
op-9b21.state      = closed
op-9b21.resolution = completed
op-9b21.closed_by  = @codex-pcc
```

> StateMachine: `executing → completed` (task vocab) 통과.
> 자동 effect: parent op-3a4f 의 metadata 에 children_resolved 카운터 +1 (Scenario 1 inquiry 가 이미 closed 라 시각적 효과만, 향후 G5 hierarchy 에서 의미 갖는다).

---

## 종합 — 한 시나리오에서 사용된 v2 feature 매트릭스

|                                  | Scenario 1 | Scenario 2 | Scenario 3 |
|----------------------------------|:---:|:---:|:---:|
| operations_v2 (kind 다양)        | inquiry | proposal | task w/ parent |
| operation_events_v2 (seq 단조)   | 12 | 9 | 18 |
| participants 자동 등록 (addressed)| ✓ | ✓ | ✓ |
| whisper (private_to_actor_ids)   | 1 | 2 | 1 |
| operation_artifacts_v2 첨부      | 2 | 0 | 4 |
| reply chain (replies_to_event_id)| ✓ | ✓ | ✓ |
| StateMachine close vocab 검증    | answered | accepted | completed |
| Capability 동적 grant (G1+F9)    | -- | -- | task.approve.destructive |
| broker fan-out per-actor (G3)    | 3 actors | 3 actors | 4 actors |
| inbox redaction (v2 reader)      | claude-pcb 1건 | operator 2건 | claude-pcb 1건 |
| v1 reader redaction (G4)         | 자동 | 자동 | 자동 |

---

## 운영자가 꺼낼 수 있는 사후 쿼리

```bash
# 가장 손이 많이 든 op 의 timeline 다시 보기
GET /v2/operations/op-9b21/events?actor_handle=@operator
# (operator 의 시점이라 whisper 다 보임)

# claude-pcb 의 inbox 에서 미독 op
GET /v2/inbox?actor_handle=@claude-pcb&state=open
# (Scenario 1,2 닫혔으니 빈 결과; 새 inquiry 오면 채워짐)

# 전체 trace 스트림으로 듣고 싶으면
GET /v2/inbox/stream?actor_handle=@operator
# (heartbeat 마다 keepalive, v2.event 로 새 이벤트 도착)

# Scenario 3 의 artifact 한 줄로 다 모으기
GET /v2/operations/op-9b21/artifacts
# -> [{kind:patch,...},{kind:log,...},{kind:metric_snapshot,...},{kind:log,...}]

# 부모 inquiry 의 자식 task 가 어떤 게 있는지
SELECT * FROM operations_v2 WHERE parent_operation_id = 'op-3a4f';
# -> op-9b21 (G5 단계에서 API 화 예정)
```

---

위 trace 는 *프로토콜이 어떻게 보이게 만들어졌는지* 의 가상 예시. 실제 `tests/test_kernel_v2_m1_multi_agent.py` 가 in-process 로 비슷한 형태의 사이클을 매 회귀에서 검증한다.
