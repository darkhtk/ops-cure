"""Chat behavior Discord message handling."""

from __future__ import annotations

import discord

from ...transports.discord.threads import ThreadManager
from .service import ChatBehaviorService


class ChatDiscordMessageHandler:
    def __init__(self, *, chat_service: ChatBehaviorService, thread_manager: ThreadManager) -> None:
        self.chat_service = chat_service
        self.thread_manager = thread_manager

    async def handle_discord_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        summary = self.chat_service.record_message(
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
                    "Codex 대화 상태\n"
                    f"- turns: `{summary.turn_count}`\n"
                    f"- last actor: `{summary.last_actor_name or 'n/a'}`\n"
                    f"- last preview: `{summary.last_message_preview or 'n/a'}`"
                ),
            )
