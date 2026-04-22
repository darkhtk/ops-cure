"""Discord transport contracts for behavior plugins."""

from __future__ import annotations

from typing import Protocol

import discord
from discord import app_commands


class DiscordCommandProvider(Protocol):
    """Registers transport-visible commands for a behavior."""

    def register_commands(self, tree: app_commands.CommandTree) -> None:
        ...


class DiscordMessageHandler(Protocol):
    """Consumes inbound Discord messages for a behavior."""

    async def handle_discord_message(self, message: discord.Message) -> None:
        ...
