"""Discord transport bindings for behavior plugins."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import DiscordCommandProvider, DiscordMessageHandler


@dataclass(frozen=True, slots=True)
class DiscordBehaviorBinding:
    behavior_id: str
    command_provider: DiscordCommandProvider | None = None
    message_handler: DiscordMessageHandler | None = None
