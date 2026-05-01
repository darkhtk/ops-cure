"""Agent SDK for ops-cure protocol v2.

A thin Python client an autonomous agent uses to participate in
protocol v2 collaboration rooms via the bridge HTTP API.

Two layers:

  ``BridgeV2Client``  -- raw HTTP client with bearer auth + asserted
                         client_id; surfaces every /v2 route as a
                         method.

  ``AgentRuntime``    -- loop scaffold: poll inbox, dispatch incoming
                         events to a user-supplied handler, advance
                         last_seen_seq automatically.

The SDK has NO LLM dependency. The agent owner wires their model
client (Claude / Codex / etc.) into the handler. F11's deliverable is
the protocol seam, not a specific model integration.
"""
from .client import BridgeV2Client, BridgeV2Error  # noqa: F401
from .runtime import AgentRuntime, IncomingEvent, AgentHandler  # noqa: F401

__all__ = [
    "BridgeV2Client",
    "BridgeV2Error",
    "AgentRuntime",
    "IncomingEvent",
    "AgentHandler",
]
