"""Agent behavior — in-process LLM-backed actor that participates in v2 ops.

Why this exists:
    The v2 protocol was designed so external agents can connect via
    BridgeV2Client + AgentRuntime. This behavior makes the *bridge
    itself* host one or more actor identities backed by an LLM, so a
    fresh deployment has a working `@claude-pca` (or similar) the
    moment it boots, without operators needing to spin up separate
    agent processes.

Design layers:

  - ``AgentBrain`` (Protocol): given an inbox event + context dict,
    return a list of intended actions. Pluggable so tests use a
    deterministic stub (EchoBrain) and prod uses the LLM-backed
    ClaudeBrain.

  - ``AgentRunner``: subscribes to ``v2:inbox:<actor_id>`` on the
    in-process broker, builds context (operation summary + recent
    events + (future) digest card), calls brain.respond(), then
    dispatches each returned action back through the chat service
    (which dual-writes v1+v2 + republishes via mirror chain).

  - ``AgentService``: orchestrates one or more AgentRunners as
    asyncio tasks during the bridge lifespan.

Configuration (env):
    BRIDGE_AGENT_ENABLED         "true" to spawn agents at boot
    BRIDGE_AGENT_HANDLE          actor handle this bridge represents,
                                 e.g. "@claude-pca"
    BRIDGE_AGENT_BRAIN           "claude" (default) | "echo" (test)
    BRIDGE_AGENT_MODEL           Claude model id (default opus latest)
    BRIDGE_ANTHROPIC_API_KEY     required when brain=claude
    BRIDGE_AGENT_SYSTEM_PROMPT   override default system prompt
"""
from .brains import (  # noqa: F401
    AgentBrain, EchoBrain, ClaudeBrain, PCClaudeBrain,
    _build_claude_tools, _tool_uses_to_actions,
)
from .runner import AgentRunner, ActionResult  # noqa: F401
from .service import AgentService  # noqa: F401

__all__ = [
    "AgentBrain",
    "EchoBrain",
    "ClaudeBrain",
    "AgentRunner",
    "ActionResult",
    "AgentService",
]
