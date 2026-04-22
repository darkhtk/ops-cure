"""Discord binding for generic chat behavior."""

from __future__ import annotations

from ...transports.discord.bindings import DiscordBehaviorBinding
from ...transports.discord.threads import ThreadManager
from .discord_commands import ChatDiscordCommandProvider
from .discord_messages import ChatDiscordMessageHandler
from .service import ChatBehaviorService


def build_chat_discord_binding(
    *,
    chat_service: ChatBehaviorService,
    thread_manager: ThreadManager,
) -> DiscordBehaviorBinding:
    return DiscordBehaviorBinding(
        behavior_id="chat",
        command_provider=ChatDiscordCommandProvider(chat_service=chat_service),
        message_handler=ChatDiscordMessageHandler(
            chat_service=chat_service,
            thread_manager=thread_manager,
        ),
    )
