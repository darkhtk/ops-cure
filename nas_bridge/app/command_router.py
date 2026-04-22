"""Backward-compatible orchestration command shim."""

from __future__ import annotations

from .behaviors.orchestration.discord_commands import OrchestrationDiscordCommandProvider
from .transports.discord.commands import register_commands as register_transport_commands


def register_commands(tree, *, session_service, verification_service, registry) -> None:
    register_transport_commands(
        tree,
        providers=[
            OrchestrationDiscordCommandProvider(
                session_service=session_service,
                verification_service=verification_service,
                registry=registry,
            ),
        ],
    )
