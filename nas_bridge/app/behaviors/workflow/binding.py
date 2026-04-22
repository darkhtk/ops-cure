"""Discord binding for workflow behavior."""

from __future__ import annotations

from ...kernel.registry import WorkerRegistry
from ...transports.discord.bindings import DiscordBehaviorBinding
from .discord_commands import WorkflowDiscordCommandProvider
from .discord_messages import WorkflowDiscordMessageHandler
from .service import SessionService
from .verification import VerificationService


def build_workflow_discord_binding(
    *,
    session_service: SessionService,
    verification_service: VerificationService,
    registry: WorkerRegistry,
) -> DiscordBehaviorBinding:
    return DiscordBehaviorBinding(
        behavior_id="orchestration",
        command_provider=WorkflowDiscordCommandProvider(
            session_service=session_service,
            verification_service=verification_service,
            registry=registry,
        ),
        message_handler=WorkflowDiscordMessageHandler(session_service),
    )
