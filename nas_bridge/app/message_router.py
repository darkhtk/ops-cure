"""Backward-compatible workflow message shim."""

from __future__ import annotations

from .behaviors.workflow.discord_messages import WorkflowDiscordMessageHandler
from .transports.discord.messages import MessageRouter

__all__ = ["MessageRouter", "WorkflowDiscordMessageHandler"]
