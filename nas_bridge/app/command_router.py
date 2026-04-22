"""Backward-compatible workflow command shim."""

from __future__ import annotations

from .behaviors.workflow.discord_commands import WorkflowDiscordCommandProvider
from .transports.discord.commands import register_commands as register_transport_commands


def register_commands(tree, *, session_service, verification_service, registry) -> None:
    register_transport_commands(
        tree,
        providers=[
            WorkflowDiscordCommandProvider(
                session_service=session_service,
                verification_service=verification_service,
                registry=registry,
            ),
        ],
    )
