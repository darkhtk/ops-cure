"""Ops behavior Discord message handling."""

from __future__ import annotations

import discord

from ...transports.discord.threads import ThreadManager
from .service import OpsBehaviorService


class OpsDiscordMessageHandler:
    def __init__(self, *, ops_service: OpsBehaviorService, thread_manager: ThreadManager) -> None:
        self.ops_service = ops_service
        self.thread_manager = thread_manager

    async def handle_discord_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        summary = self.ops_service.record_message(
            thread_id=str(message.channel.id),
            actor_name=message.author.display_name,
            content=message.content,
        )
        if summary is None:
            return

        normalized = message.content.strip().lower()
        if normalized in {"state", "/state", "!state"}:
            await self.thread_manager.post_message(
                str(message.channel.id),
                (
                    "Ops room 상태\n"
                    f"- status: `{summary.status}`\n"
                    f"- issues: `{summary.issue_count}`\n"
                    f"- notes: `{summary.note_count}`\n"
                    f"- last: `{summary.last_event_kind or 'n/a'}` / `{summary.last_event_preview or 'n/a'}`"
                ),
            )
