"""Chat behavior Discord command surface."""

from __future__ import annotations

import discord
from discord import app_commands

from .service import ChatBehaviorService


class ChatDiscordCommandProvider:
    def __init__(self, *, chat_service: ChatBehaviorService) -> None:
        self.chat_service = chat_service

    def register_commands(self, tree: app_commands.CommandTree) -> None:
        chat_group = app_commands.Group(name="chat", description="Open and inspect Codex-to-Codex dialogue spaces")

        @chat_group.command(name="start", description="Start a Codex dialogue in a new thread")
        @app_commands.describe(topic="Optional topic or label for the dialogue")
        async def chat_start(interaction: discord.Interaction, topic: str | None = None) -> None:
            if interaction.guild_id is None or interaction.channel_id is None:
                await interaction.response.send_message(
                    "Chat spaces can only be started from a guild channel.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            title = f"codex-chat: {(topic or 'freeform').strip()}"
            try:
                created = await self.chat_service.create_chat_thread(
                    guild_id=str(interaction.guild_id),
                    parent_channel_id=str(interaction.channel_id),
                    title=title,
                    topic=topic,
                    created_by=str(interaction.user.id),
                )
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(str(exc), ephemeral=True)
                return

            await interaction.followup.send(
                f"Chat space started in thread `<#{created.discord_thread_id}>` with id `{created.id}`.",
                ephemeral=True,
            )

        @chat_group.command(name="state", description="Show the state of the current Codex dialogue room")
        async def chat_state(interaction: discord.Interaction) -> None:
            channel = interaction.channel
            if not isinstance(channel, discord.Thread):
                await interaction.response.send_message(
                    "Run this command inside a Codex dialogue thread.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            summary = self.chat_service.get_chat_thread(thread_id=str(channel.id))
            if summary is None:
                await interaction.followup.send("This thread is not managed by the chat behavior.", ephemeral=True)
                return

            await interaction.followup.send(
                (
                    f"Chat space `{summary.title}`\n"
                    f"- status: `{summary.status}`\n"
                    f"- turns: `{summary.turn_count}`\n"
                    f"- last actor: `{summary.last_actor_name or 'n/a'}`\n"
                    f"- last preview: `{summary.last_message_preview or 'n/a'}`"
                ),
                ephemeral=True,
            )

        tree.add_command(chat_group)
