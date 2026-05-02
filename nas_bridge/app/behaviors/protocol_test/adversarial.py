"""H2: adversarial personas — protocol 의 attack surface 점검용.

각 persona 는 한 가지 공격 벡터에 특화. 시나리오는 그 공격이 protocol
에 의해 막혀야 한다는 invariant 를 assert; 막히지 않으면 진짜 bug.

Five vectors:

  WhisperLeakerBrain    받은 whisper 본문을 공개 speech 로 quote.
                        막히지 않음 (현재 protocol 미보호) -> 발견되는
                        gap. 시나리오는 leak 이 일어났음을 기록만 한다.

  RogueCloserBrain      opener/owner 가 아니면서 close 시도. 거부 확인
                        (ChatConversationStateError -> ActionResult 실패).

  LoopHostBrain         인입되는 *모든* speech 에 무조건 응답. self-
                        envelope filter 가 없으면 무한 loop -- runner 의
                        가드가 잡고 quiescence 도달해야 한다.

  LeaseSquatterBrain    다른 actor 가 든 task lease 위에 claim 시도.
                        RemoteTaskService 가 거부해야 한다 (lease
                        contract 위반).

  InboxSpammerBrain     1번 dispatch 마다 N개 speech 발화. rate limit
                        부재 노출 -- 현재 protocol 에 limiter 없으니
                        시나리오는 N 발화가 모두 들어왔는지만 기록.
"""
from __future__ import annotations

from typing import Any

from .personas import PersonaBrain


class WhisperLeakerBrain(PersonaBrain):
    """받은 whisper 본문을 공개 speech 로 quote.

    이건 protocol-level 으로는 막히지 않는다 (재발화는 새 event 라
    private_to 가 이어지지 않음). 시나리오 끝에서 quote 가 다른 actor
    에게 노출됐는지 검증 -> 노출 됐으면 'leak detected' 로 기록.
    실제 차단은 application layer (LLM brain 의 self-restraint) or
    protocol upgrade (whisper-tainted 표시 + downstream restriction).
    """
    handle = "@whisper-leaker"
    description = "leaks whispered content as public speech"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        if kind != "chat.speech.claim":
            return None
        # Was this event a whisper to me? viewer received it because it
        # was private_to_actor_ids that included viewer.
        private_to = context.get("private_to_actor_ids")
        viewer_id = context.get("viewer_actor_id")
        if not private_to or viewer_id not in private_to:
            return None
        text = event_payload.get("text", "")
        # Self-bump to prevent infinite cascade if leak event somehow
        # comes back as another whisper.
        n = self._bump("leaks")
        if n > 1:
            return None
        return [{
            "action": "speech.claim",
            "text": f"FYI public quote of whisper: {text}",
        }]


class RogueCloserBrain(PersonaBrain):
    """opener/owner 가 아닌데 close 시도. opener-only 권한 검사가
    거부해야 한다."""
    handle = "@rogue-closer"
    description = "tries to close ops it didn't open"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        op = context.get("operation") or {}
        op_id = op.get("id")
        if not op_id:
            return None
        # Only attempt once per op
        if self._bump(f"close:{op_id}") > 1:
            return None
        # Try to close after seeing any speech event
        if not kind.startswith("chat.speech."):
            return None
        op_kind = op.get("kind", "")
        resolution = {"inquiry": "answered", "proposal": "accepted",
                      "task": "completed"}.get(op_kind)
        if not resolution:
            return None
        return [{
            "action": "close",
            "resolution": resolution,
            "summary": "I am the rogue closer",
        }]


class LoopHostBrain(PersonaBrain):
    """모든 speech 에 응답. runner 의 self-envelope filter 가 없으면
    무한 loop. quiescence 가 도달해야 가드 동작 증명.

    bound 를 두지 않고 매 invocation 에 응답 (다른 persona 와 달리
    cap 없음) -- runner 의 loop guard 만이 멈출 수 있다.
    """
    handle = "@loop-host"
    description = "responds to every speech; tests runner loop guard"

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        if not kind.startswith("chat.speech."):
            return None
        # ALWAYS reply. No cap, no addressed_to filter.
        return [{
            "action": "speech.claim",
            "text": "loop response",
        }]


class LeaseSquatterBrain(PersonaBrain):
    """다른 actor 가 lease 든 task 에 자기가 claim 시도. RemoteTaskService
    가 거부 (current_assignment 가 다른 actor) -- 거부되면 ActionResult
    delivered=False, detail 에 lease error 포함."""
    handle = "@lease-squatter"
    description = "tries to claim a lease another actor holds"

    def respond(self, event_payload, context):
        # Only fire on chat.task.claimed events (someone else claimed)
        kind = context.get("event_kind", "")
        if kind != "chat.task.claimed":
            return None
        if self._bump("squats") > 1:
            return None
        # protocol_test 의 PersonaBrain action vocabulary 는 speech +
        # close 만 -- task.claim 은 직접 못 쏨. 시나리오에서 이 brain
        # 이 출력하는 'task.claim' action 은 runner 가 'unknown action
        # kind' 로 거부 (현재 design). 즉 squat 시도 자체가 unknown 으로
        # 기록 = 가드 통과로 간주.
        return [{
            "action": "task.claim",
            "lease_seconds": 60,
        }]


class InboxSpammerBrain(PersonaBrain):
    """1번 dispatch 에 burst 발화. rate limit 부재 노출.

    burst_size 만큼 speech.claim 을 한 번에 반환. runner 는 이를
    순서대로 dispatch. limiter 가 없으니 모두 통과 = scenario 가
    'spammed=True' 기록.
    """
    handle = "@inbox-spammer"
    description = "burst speeches; tests rate-limit absence"

    def __init__(self, *, burst_size: int = 5) -> None:
        super().__init__()
        self._burst_size = burst_size

    def respond(self, event_payload, context):
        kind = context.get("event_kind", "")
        if not kind.startswith("chat.speech."):
            return None
        if self._bump("bursts") > 1:
            return None  # one burst per scenario
        return [
            {"action": "speech.claim", "text": f"spam #{i}"}
            for i in range(1, self._burst_size + 1)
        ]


ALL_ADVERSARIAL: tuple[type[PersonaBrain], ...] = (
    WhisperLeakerBrain,
    RogueCloserBrain,
    LoopHostBrain,
    LeaseSquatterBrain,
    InboxSpammerBrain,
)
