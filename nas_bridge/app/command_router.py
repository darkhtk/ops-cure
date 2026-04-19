from __future__ import annotations

import discord
from discord import app_commands

from .session_service import SessionService
from .worker_registry import WorkerRegistry


def register_commands(
    tree: app_commands.CommandTree,
    *,
    session_service: SessionService,
    registry: WorkerRegistry,
) -> None:
    project_group = app_commands.Group(name="project", description="Manage project sessions")
    agent_group = app_commands.Group(name="agent", description="Manage individual agents")
    session_group = app_commands.Group(name="session", description="Manage the active session")

    async def _current_thread_session(interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            raise app_commands.AppCommandError("Run this command inside a managed session thread.")
        return await session_service.get_session_summary_by_thread(str(channel.id))

    @project_group.command(name="start", description="Start a new project session")
    @app_commands.describe(name="New session name", preset="Optional worker preset from registered YAML")
    async def project_start(interaction: discord.Interaction, name: str, preset: str | None = None) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Project sessions can only be started from a guild channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await session_service.create_session_from_project(
                project_name=name,
                preset=preset,
                user_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id),
                parent_channel_id=str(interaction.channel_id),
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await interaction.followup.send(
            f"Session started in thread `<#{summary.discord_thread_id}>` with id `{summary.id}`.",
            ephemeral=True,
        )

    @project_group.command(name="find", description="Search configured local roots and resume a matching project")
    @app_commands.describe(query="Describe the project or folder to resume", preset="Optional worker preset from registered YAML")
    async def project_find(interaction: discord.Interaction, query: str, preset: str | None = None) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Project search can only be started from a guild channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            find_summary = await session_service.enqueue_project_find(
                query_text=query,
                preset=preset,
                user_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id),
                parent_channel_id=str(interaction.channel_id),
            )
            resolved = await session_service.wait_for_project_find(find_id=find_summary.id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if resolved is None:
            await interaction.followup.send(
                "Project search is still running on the PC launcher. Try `/project find` again in a moment.",
                ephemeral=True,
            )
            return

        if resolved.status == "selected" and resolved.selected_path:
            session_name = resolved.selected_name or query.strip() or "resumed-project"
            try:
                summary = await session_service.create_session_from_project(
                    project_name=session_name,
                    preset=resolved.preset,
                    user_id=str(interaction.user.id),
                    guild_id=str(interaction.guild_id),
                    parent_channel_id=str(interaction.channel_id),
                    workdir_override=resolved.selected_path,
                )
                await session_service.mark_project_find_started(find_id=resolved.id, session_summary=summary)
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(
                    f"Project was found at `{resolved.selected_path}`, but session start failed: {exc}",
                    ephemeral=True,
                )
                return

            reason_suffix = f"\nReason: {resolved.reason}" if resolved.reason else ""
            await interaction.followup.send(
                (
                    f"Found `{resolved.selected_name or session_name}` at `{resolved.selected_path}`"
                    f" and started thread `<#{summary.discord_thread_id}>`."
                    f"{reason_suffix}"
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(_format_find_summary(resolved), ephemeral=True)

    @project_group.command(name="status", description="Show the current session status")
    async def project_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            session_summary = await _current_thread_session(interaction)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        agents = "\n".join(
            (
                f"- `{agent.agent_name}` {agent.status} [{agent.cli_type}]"
                f"{_format_drift_suffix(agent)}"
            )
            for agent in session_summary.agents
        )
        await interaction.followup.send(
            (
                f"Session `{session_summary.id}`\n"
                f"Project: `{session_summary.project_name}`\n"
                f"Preset: `{session_summary.preset or 'unknown'}`\n"
                f"Status: `{session_summary.status}`\n"
                f"Launcher: `{session_summary.launcher_id or 'unclaimed'}`\n"
                f"Agents:\n{agents}"
            ),
            ephemeral=True,
        )

    @project_group.command(name="close", description="Close the current session")
    async def project_close(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.followup.send("Run this command inside a managed session thread.", ephemeral=True)
            return
        try:
            summary = await session_service.close_session(str(channel.id), str(interaction.user.id))
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(f"Closed session `{summary.id}`.", ephemeral=True)

    @tree.command(name="agents", description="List agents in the current session")
    async def agents(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        lines = []
        for agent in summary.agents:
            default_marker = " default" if agent.is_default else ""
            lines.append(
                (
                    f"- `{agent.agent_name}` {agent.status} [{agent.cli_type}] "
                    f"{agent.role}{default_marker}{_format_drift_suffix(agent)}"
                ),
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @agent_group.command(name="restart", description="Restart a single agent")
    @app_commands.describe(name="Agent name to restart")
    async def agent_restart(interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            await session_service.enqueue_restart(summary.id, name, str(interaction.user.id))
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(f"Restart queued for `{name}`.", ephemeral=True)

    @session_group.command(name="reset", description="Clear pending jobs and restart all agents")
    async def session_reset(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            await session_service.reset_session(summary.id, str(interaction.user.id))
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Session reset queued for all agents.", ephemeral=True)

    tree.add_command(project_group)
    tree.add_command(agent_group)
    tree.add_command(session_group)


def _format_drift_suffix(agent) -> str:
    if getattr(agent, "drift_state", "unknown") == "ok":
        return ""
    reason = getattr(agent, "drift_reason", None)
    if not reason:
        return f" | drift={agent.drift_state}"
    compact = " ".join(reason.split())
    if len(compact) > 120:
        compact = compact[:117].rstrip() + "..."
    return f" | drift={agent.drift_state} ({compact})"


def _format_find_summary(summary) -> str:
    status_label = {
        "needs_clarification": "Multiple likely matches were found.",
        "no_match": "No convincing local project match was found.",
        "failed": "The local project finder failed.",
        "claimed": "The local project finder is still running.",
        "pending": "The local project finder is still queued.",
        "started": "The matching project has already been resumed.",
    }.get(summary.status, f"Search finished with status `{summary.status}`.")
    lines = [status_label]
    if getattr(summary, "reason", None):
        lines.append(f"Reason: {summary.reason}")
    candidates = getattr(summary, "candidates", None) or []
    if candidates:
        lines.append("Top candidates:")
        for candidate in candidates[:3]:
            rationale = f" - {candidate.rationale}" if getattr(candidate, "rationale", None) else ""
            lines.append(f"- `{candidate.display_name}` at `{candidate.path}`{rationale}")
    return "\n".join(lines)
