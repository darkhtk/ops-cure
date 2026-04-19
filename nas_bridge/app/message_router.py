from __future__ import annotations

import logging

import discord

from .session_service import SessionService

LOGGER = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, session_service: SessionService) -> None:
        self.session_service = session_service

    async def handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        if not await self.session_service.get_session_exists_by_thread_id(str(message.channel.id)):
            return

        reply_message_id: str | None = None
        reply_content: str | None = None
        if message.reference is not None and message.reference.message_id is not None:
            reply_message_id = str(message.reference.message_id)
            resolved = message.reference.resolved
            if isinstance(resolved, discord.Message):
                reply_content = resolved.content

        try:
            agent_name = await self.session_service.route_discord_message(
                thread_id=str(message.channel.id),
                discord_message_id=str(message.id),
                user_id=str(message.author.id),
                content=message.content,
                author_name=message.author.display_name,
                reply_message_id=reply_message_id,
                reply_content=reply_content,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Message routing failed for thread %s: %s", message.channel.id, exc)
            await message.channel.send(str(exc))
            return

        try:
            await message.add_reaction("\u23f3")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to add queue reaction for message %s: %s", message.id, exc)
        LOGGER.info("Queued message %s for agent %s", message.id, agent_name)
