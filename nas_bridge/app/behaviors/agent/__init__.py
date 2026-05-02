"""Agent test fixtures (in-process AgentRunner + brains).

The bridge no longer hosts agents in-process. External agents connect
via /v2/inbox/stream + /v2/operations/{id}/events (see
pc_launcher/connectors/claude_executor/agent_loop.py).

What's left here is reused by:
  - protocol_test scenarios (drive personas through the broker without
    booting external processes)
  - unit tests that need a deterministic AgentBrain

Production lifespan (main.py) does NOT spawn anything from this module.
"""
from .brains import (  # noqa: F401
    AgentBrain, EchoBrain, ClaudeBrain,
    _build_claude_tools, _tool_uses_to_actions,
)
from .runner import AgentRunner, ActionResult  # noqa: F401

__all__ = [
    "AgentBrain",
    "EchoBrain",
    "ClaudeBrain",
    "AgentRunner",
    "ActionResult",
]
