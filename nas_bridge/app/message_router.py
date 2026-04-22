"""Backward-compatible orchestration message shim."""

from __future__ import annotations

from .behaviors.orchestration.discord_messages import OrchestrationDiscordMessageHandler
from .transports.discord.messages import MessageRouter

__all__ = ["MessageRouter", "OrchestrationDiscordMessageHandler"]
