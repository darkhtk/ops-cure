"""Ops behavior Discord command surface."""

from __future__ import annotations

import discord
from discord import app_commands

from .service import OpsBehaviorService


class OpsDiscordCommandProvider:
    def __init__(self, *, ops_service: OpsBehaviorService) -> None:
        self.ops_service = ops_service

    def register_commands(self, tree: app_commands.CommandTree) -> None:
        ops_group = app_commands.Group(name="ops", description="Open and inspect lightweight ops rooms")

        @ops_group.command(name="start", description="Start a lightweight ops room in a new thread")
        @app_commands.describe(summary="Short summary of the issue or room purpose")
        async def ops_start(interaction: discord.Interaction, summary: str) -> None:
            if interaction.guild_id is None or interaction.channel_id is None:
                await interaction.response.send_message(
                    "Ops rooms can only be started from a guild channel.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            title = f"ops: {summary.strip()[:60]}"
            try:
                created = await self.ops_service.create_ops_thread(
                    guild_id=str(interaction.guild_id),
                    parent_channel_id=str(interaction.channel_id),
                    title=title,
                    summary=summary,
                    created_by=str(interaction.user.id),
                )
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(str(exc), ephemeral=True)
                return

            await interaction.followup.send(
                f"Ops room started in thread `<#{created.discord_thread_id}>` with id `{created.id}`.",
                ephemeral=True,
            )

        @ops_group.command(name="state", description="Show the state of the current ops room")
        async def ops_state(interaction: discord.Interaction) -> None:
            channel = interaction.channel
            if not isinstance(channel, discord.Thread):
                await interaction.response.send_message(
                    "Run this command inside an ops thread.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            summary = self.ops_service.get_ops_thread(thread_id=str(channel.id))
            if summary is None:
                await interaction.followup.send("This thread is not managed by the ops behavior.", ephemeral=True)
                return

            await interaction.followup.send(
                (
                    f"Ops room `{summary.title}`\n"
                    f"- status: `{summary.status}`\n"
                    f"- issues: `{summary.issue_count}`\n"
                    f"- notes: `{summary.note_count}`\n"
                    f"- last actor: `{summary.last_actor_name or 'n/a'}`\n"
                    f"- last event: `{summary.last_event_kind or 'n/a'}` / `{summary.last_event_preview or 'n/a'}`"
                ),
                ephemeral=True,
            )

        tree.add_command(ops_group)
