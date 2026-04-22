"""Behavior catalog/introspection helpers."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from ..kernel.bindings import KernelBehaviorBinding
from ..transports.discord.bindings import DiscordBehaviorBinding


class BehaviorSummary(BaseModel):
    behavior_id: str
    display_name: str
    description: str
    supports_discord_commands: bool
    supports_discord_messages: bool
    supports_spaces: bool
    supports_actors: bool
    supports_events: bool


class BehaviorCatalogService:
    def __init__(
        self,
        *,
        descriptors: Iterable,
        kernel_bindings: Iterable[KernelBehaviorBinding],
        discord_bindings: Iterable[DiscordBehaviorBinding],
    ) -> None:
        self.descriptors = tuple(descriptors)
        self.kernel_binding_map = {binding.behavior_id: binding for binding in kernel_bindings}
        self.discord_binding_map = {binding.behavior_id: binding for binding in discord_bindings}

    def list_behaviors(self) -> list[BehaviorSummary]:
        summaries: list[BehaviorSummary] = []
        for descriptor in self.descriptors:
            kernel_binding = self.kernel_binding_map.get(descriptor.behavior_id)
            discord_binding = self.discord_binding_map.get(descriptor.behavior_id)
            summaries.append(
                BehaviorSummary(
                    behavior_id=descriptor.behavior_id,
                    display_name=descriptor.display_name,
                    description=descriptor.description,
                    supports_discord_commands=bool(
                        discord_binding is not None and discord_binding.command_provider is not None,
                    ),
                    supports_discord_messages=bool(
                        discord_binding is not None and discord_binding.message_handler is not None,
                    ),
                    supports_spaces=bool(
                        kernel_binding is not None and kernel_binding.space_provider is not None,
                    ),
                    supports_actors=bool(
                        kernel_binding is not None and kernel_binding.actor_provider is not None,
                    ),
                    supports_events=bool(
                        kernel_binding is not None and kernel_binding.event_provider is not None,
                    ),
                ),
            )
        return summaries
