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
    "You are addressed by handle (e.g. @claude-pca). When you have something "
    "useful to say, call exactly one tool: speech_claim for assertions, "
    "speech_question for follow-ups, speech_object to push back, "
    "speech_propose for proposals, etc. Use addressed_to when speaking to "
    "a specific actor; use private_to_actors only for sensitive notes. "
    "If the question is out of scope or requires no reply, do not call "
    "any tool. Be concise (1-3 sentences)."
)


def _build_claude_tools() -> list[dict[str, object]]:
    """H4: derive Claude tool definitions from contract.SPEECH_KINDS +
    a static close_operation tool. Single source means a new SpeechKind
    automatically becomes a tool the brain can use."""
    from ...kernel.v2 import contract as _v2_contract

    tools: list[dict[str, object]] = []
    for kind in sorted(_v2_contract.SPEECH_KINDS):
        tools.append({
            "name": f"speech_{kind}",
            "description": f"Submit a chat.speech.{kind} message to the operation.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The speech content (1-3 sentences).",
                    },
                    "addressed_to": {
                        "type": "string",
                        "description": (
                            "Handle (without @) of a specific actor to address. "
                            "Omit to broadcast to all participants."
                        ),
                    },
                    "private_to_actors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Whisper: only these handles will see the content. "
                            "Use ONLY for sensitive notes."
                        ),
                    },
                },
                "required": ["text"],
            },
        })
    tools.append({
        "name": "close_operation",
        "description": (
            "Close the operation with a kind-appropriate resolution. "
            "Only available when this brain owns / opened the op."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resolution": {
                    "type": "string",
                    "description": (
                        "Per-kind vocab: inquiry=answered/dropped/escalated, "
                        "proposal=accepted/rejected/withdrawn/superseded, "
                        "task=completed/failed/cancelled."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "Short closure note.",
                },
            },
            "required": ["resolution"],
        },
    })
    return tools


def _tool_uses_to_actions(tool_uses: list[dict[str, object]]) -> list[dict[str, object]]:
    """Translate a list of Claude tool_use blocks (already extracted to
    dict form) into AgentBrain actions. Pure function -- no SDK
    dependency, fully unit-testable."""
    actions: list[dict[str, object]] = []
    for use in tool_uses:
        name = str(use.get("name") or "")
        inp = use.get("input") or {}
        if not isinstance(inp, dict):
            inp = {}
        if name.startswith("speech_"):
            kind = name[len("speech_"):]
            text = str(inp.get("text") or "").strip()
            if not text:
                continue
            entry: dict[str, object] = {
                "action": f"speech.{kind}",
                "text": text,
            }
            if inp.get("addressed_to"):
                entry["addressed_to"] = str(inp["addressed_to"])
            priv = inp.get("private_to_actors")
            if priv and isinstance(priv, list):
                entry["private_to_actors"] = [str(x) for x in priv]
            actions.append(entry)
        elif name == "close_operation":
            resolution = str(inp.get("resolution") or "").strip()
            if not resolution:
                continue
            entry = {"action": "close", "resolution": resolution}
            if inp.get("summary"):
                entry["summary"] = str(inp["summary"])
            actions.append(entry)
        # unknown tool name: silently skip (brain hallucinated)
    return actions


class ClaudeBrain:
    """Anthropic Claude-backed brain. Tool-use mode (H4): the model
    chooses speech_<kind> or close_operation rather than emitting raw
    text. Tools are derived from contract.SPEECH_KINDS so adding a new
    speech kind to the protocol automatically extends the brain's
    vocabulary."""

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
        self._tools = _build_claude_tools()

    def _build_messages(
        self,
        event_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        my_actor_id = context.get("viewer_actor_id")
        recent = context.get("recent_events", [])
        op = context.get("operation", {})
        messages: list[dict[str, Any]] = []
        for ev in recent[-self._history_limit:]:
            text = (ev.get("payload") or {}).get("text") or ""
            if not text:
                continue
            role = "assistant" if ev.get("actor_id") == my_actor_id else "user"
            messages.append({"role": role, "content": text})
        if not messages or messages[-1]["role"] != "user":
            trigger_text = event_payload.get("text", "")
            messages.append({"role": "user", "content": trigger_text or "(no text)"})
        op_intro = (
            f"[operation: {op.get('kind', 'unknown')} -- '{op.get('title', '')}']"
        )
        for m in messages:
            if m["role"] == "user":
                m["content"] = f"{op_intro}\n\n{m['content']}"
                break
        return messages

    def respond(
        self,
        event_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        # Lifecycle events (opened/closed/handoff) are spectator-only.
        if not context.get("event_kind", "").startswith("chat.speech."):
            return None
        messages = self._build_messages(event_payload, context)
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._system,
                tools=self._tools,
                messages=messages,
            )
        except Exception:  # noqa: BLE001 -- log and ignore; never crash the loop
            logger.exception("ClaudeBrain.respond failed")
            return None

        # Extract tool_use blocks. The SDK returns content as a list of
        # ContentBlock objects with .type / .name / .input attrs.
        tool_uses: list[dict[str, object]] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                tool_uses.append({
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                })
        actions = _tool_uses_to_actions(tool_uses)
        return actions or None
