from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

import discord

from .config import Settings

LOGGER = logging.getLogger(__name__)
DISCORD_MESSAGE_LIMIT = 1800


def chunk_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> Iterable[str]:
    clean_text = text.strip()
    if not clean_text:
        yield "(no output)"
        return
    for index in range(0, len(clean_text), limit):
        yield clean_text[index : index + limit]


class ThreadManager:
    def __init__(self, settings: Settings, discord_client: discord.Client | None = None) -> None:
        self.settings = settings
        self.discord_client = discord_client

    def bind_client(self, discord_client: discord.Client) -> None:
        self.discord_client = discord_client

    @property
    def discord_enabled(self) -> bool:
        return not self.settings.disable_discord and self.discord_client is not None

    async def create_session_thread(
        self,
        *,
        guild_id: str,
        parent_channel_id: str,
        project_name: str,
        template: str,
        auto_archive_duration: int,
    ) -> str:
        if not self.discord_enabled:
            fake_id = f"dev-thread-{project_name}-{int(datetime.utcnow().timestamp())}"
            LOGGER.info("Discord disabled, using fake thread id %s", fake_id)
            return fake_id

        assert self.discord_client is not None
        parent_channel = self.discord_client.get_channel(int(parent_channel_id))
        if parent_channel is None:
            parent_channel = await self.discord_client.fetch_channel(int(parent_channel_id))

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        thread_name = template.format(project_name=project_name, timestamp=timestamp)

        if isinstance(parent_channel, discord.TextChannel):
            starter = await parent_channel.send(f"Session starting for `{project_name}`.")
            thread = await starter.create_thread(
                name=thread_name,
                auto_archive_duration=auto_archive_duration,
            )
            return str(thread.id)

        if isinstance(parent_channel, discord.ForumChannel):
            thread_with_message = await parent_channel.create_thread(
                name=thread_name,
                content=f"Session starting for `{project_name}`.",
                auto_archive_duration=auto_archive_duration,
            )
            return str(thread_with_message.thread.id)

        raise TypeError(
            f"Unsupported parent channel type: {type(parent_channel).__name__}",
        )

    async def post_message(self, thread_id: str, text: str) -> list[tuple[str, str]]:
        if not self.discord_enabled:
            LOGGER.info("Discord disabled, thread %s message: %s", thread_id, text)
            return []

        assert self.discord_client is not None
        channel = self.discord_client.get_channel(int(thread_id))
        if channel is None:
            channel = await self.discord_client.fetch_channel(int(thread_id))

        sent_chunks: list[tuple[str, str]] = []
        for chunk in chunk_text(text):
            sent_message = await channel.send(chunk)
            sent_chunks.append((str(sent_message.id), chunk))
        return sent_chunks

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> tuple[str, str] | None:
        if not self.discord_enabled:
            LOGGER.info("Discord disabled, edit thread %s message %s: %s", thread_id, message_id, text)
            return (message_id, text)

        channel = await self._load_thread(thread_id)
        if channel is None:
            return None

        chunk = next(iter(chunk_text(text)), "(no output)")
        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Unable to load message %s in thread %s for edit: %s", message_id, thread_id, exc)
            return None

        try:
            await message.edit(content=chunk)
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Unable to edit message %s in thread %s: %s", message_id, thread_id, exc)
            return None
        return (str(message.id), chunk)

    async def archive_thread(self, thread_id: str, reason: str) -> None:
        if not self.discord_enabled:
            LOGGER.info("Discord disabled, archive thread %s (%s)", thread_id, reason)
            return

        thread = await self._load_thread(thread_id)
        if isinstance(thread, discord.Thread):
            await thread.edit(archived=True, locked=False, reason=reason)

    async def cleanup_thread(self, thread_id: str, reason: str) -> str:
        if not self.discord_enabled:
            LOGGER.info("Discord disabled, cleanup thread %s (%s)", thread_id, reason)
            return "disabled"

        thread = await self._load_thread(thread_id)
        if not isinstance(thread, discord.Thread):
            return "missing"

        try:
            await thread.delete(reason=reason)
            return "deleted"
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Thread delete failed for %s: %s", thread_id, exc)

        try:
            await thread.edit(archived=True, locked=False, reason=reason)
            return "archived"
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Thread archive fallback failed for %s: %s", thread_id, exc)
            return "failed"

    async def probe_thread_state(self, thread_id: str) -> str:
        if not self.discord_enabled:
            return "exists"

        assert self.discord_client is not None
        thread = self.discord_client.get_channel(int(thread_id))
        if thread is not None:
            return "exists"

        try:
            await self.discord_client.fetch_channel(int(thread_id))
        except discord.NotFound:
            return "missing"
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Unable to probe thread %s: %s", thread_id, exc)
            return "unknown"
        return "exists"

    async def _load_thread(self, thread_id: str):
        assert self.discord_client is not None
        thread = self.discord_client.get_channel(int(thread_id))
        if thread is not None:
            return thread
        try:
            return await self.discord_client.fetch_channel(int(thread_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Unable to load thread %s: %s", thread_id, exc)
            return None
