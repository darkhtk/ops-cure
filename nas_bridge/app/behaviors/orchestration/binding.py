"""Discord binding for the public orchestration behavior."""

from __future__ import annotations

from ...kernel.registry import WorkerRegistry
from ...transports.discord.bindings import DiscordBehaviorBinding
from .discord_commands import OrchestrationDiscordCommandProvider
from .discord_messages import OrchestrationDiscordMessageHandler
from .service import SessionService
from .verification import VerificationService


def build_orchestration_discord_binding(
    *,
    session_service: SessionService,
    verification_service: VerificationService,
    registry: WorkerRegistry,
) -> DiscordBehaviorBinding:
    return DiscordBehaviorBinding(
        behavior_id="orchestration",
        command_provider=OrchestrationDiscordCommandProvider(
            session_service=session_service,
            verification_service=verification_service,
            registry=registry,
        ),
        message_handler=OrchestrationDiscordMessageHandler(session_service),
    )


__all__ = ["build_orchestration_discord_binding"]
