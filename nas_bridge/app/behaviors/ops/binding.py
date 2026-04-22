"""Discord binding for lightweight ops behavior."""

from __future__ import annotations

from ...transports.discord.bindings import DiscordBehaviorBinding
from ...transports.discord.threads import ThreadManager
from .discord_commands import OpsDiscordCommandProvider
from .discord_messages import OpsDiscordMessageHandler
from .service import OpsBehaviorService


def build_ops_discord_binding(
    *,
    ops_service: OpsBehaviorService,
    thread_manager: ThreadManager,
) -> DiscordBehaviorBinding:
    return DiscordBehaviorBinding(
        behavior_id="ops",
        command_provider=OpsDiscordCommandProvider(ops_service=ops_service),
        message_handler=OpsDiscordMessageHandler(
            ops_service=ops_service,
            thread_manager=thread_manager,
        ),
    )
