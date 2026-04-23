"""Future Discord coordination mirror hook for remote_codex."""

from __future__ import annotations

from ...transports.discord.bindings import DiscordBehaviorBinding


def build_remote_codex_discord_binding() -> DiscordBehaviorBinding:
    return DiscordBehaviorBinding(behavior_id="remote_codex")
