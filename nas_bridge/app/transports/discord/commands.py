"""Discord command registration adapter."""

from __future__ import annotations

from collections.abc import Iterable

from discord import app_commands

from .contracts import DiscordCommandProvider


def register_commands(
    tree: app_commands.CommandTree,
    *,
    providers: Iterable[DiscordCommandProvider],
) -> None:
    for provider in providers:
        provider.register_commands(tree)


__all__ = ["register_commands"]
