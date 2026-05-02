"""Pluggable brains: stub for tests, Claude-backed for prod."""
from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("opscure.agent.brain")


class AgentBrain(Protocol):
    """Returns a list of actions in response to an inbox event.

    ``event_payload`` is the wrapped JSON the broker delivered (already
    parsed into dict). ``context`` carries operation metadata + recent
    events the agent should consider. Each returned action is a dict:

        {"action": "speech.claim", "text": "..."}
        {"action": "speech.question", "text": "...", "addressed_to": "@..."}
        {"action": "ignore"}

    Returning ``None`` or an empty list also means ignore. Brains MUST
    be deterministic-or-side-effect-free if pure; the LLM-backed brain
    obviously is not, but it shouldn't write to the bridge directly.
    """
    def respond(
        self,
        event_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]] | None: ...


class EchoBrain:
    """Deterministic test brain. Replies to speech.question with an
    'echo: <text>' speech.claim. Ignores everything else. The runner
    flow tests use this so behavior is reproducible."""

    def respond(
        self,
        event_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if context.get("event_kind") != "chat.speech.question":
            return None
        text = event_payload.get("text", "")
        return [{"action": "speech.claim", "text": f"echo: {text}"}]


DEFAULT_SYSTEM_PROMPT = (
    "You are an AI agent participating in an ops-cure collaboration room. "
    "You are addressed by handle (e.g. @claude-pca). Reply concisely (1-3 "
    "sentences). When you are uncertain, say so. Do not fabricate "
    "evidence. If the question is out of scope, suggest who should be "
    "addressed instead. Output ONLY the reply text, no JSON, no preamble."
)


class ClaudeBrain:
    """Anthropic Claude-backed brain. Soft-imports the SDK so the bridge
    boots even when the package isn't installed (the brain just refuses
    to instantiate)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-opus-4-7",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tokens: int = 1024,
        history_limit: int = 12,
    ) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover -- env dependent
            raise RuntimeError(
                "anthropic SDK is not installed; install via "
                "`pip install anthropic` or pick brain=echo"
            ) from exc
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._system = system_prompt
        self._max_tokens = max_tokens
        self._history_limit = history_limit

    def respond(
        self,
        event_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        # Only react to questions and addressed claims. Lifecycle
        # events (opened/closed/handoff) are spectator-only for the
        # default agent.
        if not context.get("event_kind", "").startswith("chat.speech."):
            return None
        my_actor_id = context.get("viewer_actor_id")
        recent = context.get("recent_events", [])
        op = context.get("operation", {})

        # Build a Claude message history. Treat self-authored events
        # as 'assistant', everything else as 'user'. This keeps the
        # turn-taking shape Claude expects.
        messages: list[dict[str, Any]] = []
        for ev in recent[-self._history_limit:]:
            text = (ev.get("payload") or {}).get("text") or ""
            if not text:
                continue
            role = "assistant" if ev.get("actor_id") == my_actor_id else "user"
            messages.append({"role": role, "content": text})

        # Ensure the message list ends on a user turn (Claude requires this).
        if not messages or messages[-1]["role"] != "user":
            trigger_text = event_payload.get("text", "")
            messages.append({"role": "user", "content": trigger_text or "(no text)"})

        op_intro = (
            f"[operation: {op.get('kind', 'unknown')} -- '{op.get('title', '')}']"
        )
        # Prepend op_intro to the FIRST user turn so Claude knows scope
        # without pretending it's a separate turn.
        for m in messages:
            if m["role"] == "user":
                m["content"] = f"{op_intro}\n\n{m['content']}"
                break

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._system,
                messages=messages,
            )
        except Exception:  # noqa: BLE001 -- log and ignore; never crash the loop
            logger.exception("ClaudeBrain.respond failed")
            return None

        reply_text = ""
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                reply_text += text
        reply_text = reply_text.strip()
        if not reply_text:
            return None
        return [{"action": "speech.claim", "text": reply_text}]
