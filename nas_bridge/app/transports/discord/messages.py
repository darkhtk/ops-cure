"""Discord inbound message routing adapter."""

from __future__ import annotations

from collections.abc import Iterable

import discord

from .contracts import DiscordMessageHandler


class CompositeDiscordMessageHandler:
    def __init__(self, handlers: Iterable[DiscordMessageHandler]) -> None:
        self.handlers = tuple(handlers)

    async def handle_discord_message(self, message: discord.Message) -> None:
        for handler in self.handlers:
            await handler.handle_discord_message(message)


class MessageRouter:
    def __init__(self, handler: DiscordMessageHandler) -> None:
        self.handler = handler

    async def handle_message(self, message: discord.Message) -> None:
        await self.handler.handle_discord_message(message)


__all__ = ["CompositeDiscordMessageHandler", "MessageRouter"]
