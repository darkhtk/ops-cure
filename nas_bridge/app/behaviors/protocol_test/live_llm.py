"""Live-LLM scenarios — ScenarioDriver + ClaudeBrain personas.

Opt-in (BRIDGE_ANTHROPIC_API_KEY required). Each ClaudeBrain instance
gets a distinct system prompt so they behave like different personas.
The cooperative invariants from `service.py` (op closes, no infinite
loop, redaction respected) are asserted against real LLM output.

Cost: 2-3 API calls per scenario, ~$0.01-0.02 per run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .driver import ScenarioDriver, PersonaSpec, ProtocolObservation


INVESTIGATOR_PROMPT = (
    "You are @claude-investigator, an SRE expert. When asked a "
    "question, call exactly one tool: speech_claim with a 1-2 sentence "
    "hypothesis. If you are highly uncertain, call speech_question for "
    "more information instead. Never call close_operation."
)

REVIEWER_PROMPT = (
    "You are @claude-reviewer, a careful peer reviewer. When you see "
    "a speech_claim that makes a factual assertion, call speech_object "
    "if you have ANY concern about it (1 sentence). If the claim looks "
    "fine, call no tool. Never call close_operation."
)

OPERATOR_PROMPT = (
    "You are @claude-operator. After 2 substantive replies in this op, "
    "call close_operation with resolution=answered and a 1-sentence "
    "summary. Until then, call no tool."
)


@dataclass(frozen=True)
class LiveLLMScenarioReport:
    api_calls: int
    claude_actions: int
    final_state: str
    final_resolution: str | None
    rounds_to_quiesce: int
    hit_round_cap: bool


def build_claude_personas(
    api_key: str,
    *,
    model: str = "claude-opus-4-7",
    max_tokens: int = 300,
) -> list[PersonaSpec]:
    """Three Claude-backed personas with role-distinct system prompts.
    Caller passes these to ScenarioDriver(personas=...)."""
    from ..agent.brains import ClaudeBrain

    base = {"api_key": api_key, "model": model, "max_tokens": max_tokens, "history_limit": 8}
    return [
        PersonaSpec(
            persona_cls=ClaudeBrain,
            handle="@claude-investigator",
            init_kwargs={**base, "system_prompt": INVESTIGATOR_PROMPT},
        ),
        PersonaSpec(
            persona_cls=ClaudeBrain,
            handle="@claude-reviewer",
            init_kwargs={**base, "system_prompt": REVIEWER_PROMPT},
        ),
        PersonaSpec(
            persona_cls=ClaudeBrain,
            handle="@claude-operator",
            init_kwargs={**base, "system_prompt": OPERATOR_PROMPT},
        ),
    ]


def run_live_inquiry_chain(
    *,
    chat_service,
    broker,
    api_key: str,
    model: str = "claude-opus-4-7",
    max_rounds: int = 12,
) -> tuple[ProtocolObservation, LiveLLMScenarioReport]:
    """alice asks investigator a real question. Investigator responds
    with hypothesis. Reviewer chimes in if concerned. Operator closes
    after observing replies. Cap rounds tightly to bound cost."""
    personas = build_claude_personas(api_key, model=model)
    d = ScenarioDriver(
        chat_service=chat_service,
        broker=broker,
        personas=personas,
        max_rounds=max_rounds,
    )
    thread = d.make_thread(suffix="live-llm")
    op_id = d.open_inquiry(
        opener_handle="@claude-operator",
        addressed_to_handle="@claude-investigator",
        title="why is the ci build failing?",
        discord_thread_id=thread,
        extra_participants=["@claude-reviewer"],
    )
    d.post_speech(
        operation_id=op_id,
        actor_handle="@claude-operator",
        kind="question",
        text="The CI build started failing this morning. Where would you start looking?",
        addressed_to_handle="@claude-investigator",
    )
    rounds = d.process_pending(max_rounds=max_rounds)
    obs = d.snapshot(op_id, rounds_used=rounds)

    # Sum brain_invocations across the 3 claude runners as api_calls.
    api_calls = sum(
        r.metrics["brain_invocations"]
        for r in d.runners_by_handle.values()
    )
    claude_actions = sum(
        r.metrics["actions_delivered"]
        for r in d.runners_by_handle.values()
    )
    return obs, LiveLLMScenarioReport(
        api_calls=api_calls,
        claude_actions=claude_actions,
        final_state=obs.final_state,
        final_resolution=obs.final_resolution,
        rounds_to_quiesce=obs.rounds_to_quiesce,
        hit_round_cap=obs.hit_round_cap,
    )
