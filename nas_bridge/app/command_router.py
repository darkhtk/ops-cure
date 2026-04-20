from __future__ import annotations

import discord
from discord import app_commands

from .services.verification_service import VerificationService
from .session_service import SessionService
from .worker_registry import WorkerRegistry


def register_commands(
    tree: app_commands.CommandTree,
    *,
    session_service: SessionService,
    verification_service: VerificationService,
    registry: WorkerRegistry,
) -> None:
    project_group = app_commands.Group(name="project", description="Manage project sessions")
    agent_group = app_commands.Group(name="agent", description="Manage individual agents")
    session_group = app_commands.Group(name="session", description="Manage the active session")
    policy_group = app_commands.Group(name="policy", description="Manage session policy overrides")
    verify_group = app_commands.Group(name="verify", description="Run and review verification jobs")

    async def _current_thread_session(interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            raise app_commands.AppCommandError("Run this command inside a managed session thread.")
        return await session_service.get_session_summary_by_thread(str(channel.id))

    @project_group.command(name="start", description="Start a new project session")
    @app_commands.describe(
        target="Project to open and work on",
        profile="Optional execution profile from registered YAML; defaults to sample when omitted",
        session="Optional Discord thread title override",
    )
    async def project_start(
        interaction: discord.Interaction,
        target: str,
        profile: str | None = None,
        session: str | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Project sessions can only be started from a guild channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await session_service.create_session_from_project(
                project_name=session or target,
                target_project_name=target,
                preset=profile,
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
    @app_commands.describe(query="Describe the project or folder to resume", preset="Optional execution profile from registered YAML")
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
                    target_project_name=resolved.selected_name or query.strip() or session_name,
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
            status_text = await session_service.render_session_status_text(session_summary.id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(status_text, ephemeral=True)

    @project_group.command(name="pause", description="Pause the current session")
    @app_commands.describe(reason="Optional reason shown in status and transcripts")
    async def project_pause(interaction: discord.Interaction, reason: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            paused = await session_service.pause_session(
                session_id=summary.id,
                requested_by=str(interaction.user.id),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Paused session `{paused.session_id}`. Reason: `{paused.pause_reason or 'none'}`",
            ephemeral=True,
        )

    @project_group.command(name="resume", description="Resume the current session")
    async def project_resume(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            resumed = await session_service.resume_session(
                session_id=summary.id,
                requested_by=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Resume requested for session `{resumed.session_id}`. Current status: `{resumed.status}`.",
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

    @project_group.command(name="cleanup", description="Close the current session and remove or archive its thread")
    async def project_cleanup(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.followup.send("Run this command inside a managed session thread.", ephemeral=True)
            return
        try:
            summary = await session_service.cleanup_session_thread(str(channel.id), str(interaction.user.id))
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Cleanup requested for session `{summary.id}`. The thread was removed or archived if deletion was not allowed.",
            ephemeral=True,
        )

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

    @policy_group.command(name="show", description="Show the effective session policy")
    async def policy_show(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            policy = await session_service.show_policy(session_id=summary.id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            (
                f"Policy v{policy.version} [{policy.source}] by `{policy.updated_by}`\n"
                f"- max_parallel_agents: `{policy.max_parallel_agents}`\n"
                f"- auto_retry: `{policy.auto_retry}`\n"
                f"- max_retries: `{policy.max_retries}`\n"
                f"- quiet_discord: `{policy.quiet_discord}`\n"
                f"- approval_mode: `{policy.approval_mode}`\n"
                f"- allow_cross_agent_handoff: `{policy.allow_cross_agent_handoff}`"
            ),
            ephemeral=True,
        )

    @policy_group.command(name="set", description="Override a policy value for this session")
    @app_commands.describe(key="Policy key", value="New value")
    async def policy_set(interaction: discord.Interaction, key: str, value: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            updated = await session_service.set_policy(
                session_id=summary.id,
                key=key,
                value=value,
                updated_by=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            (
                f"Updated policy `{key}` for session `{updated.session_id}`.\n"
                f"New policy version: `{updated.policy.version}`"
            ),
            ephemeral=True,
        )

    @verify_group.command(name="run", description="Queue a verification run for the current session")
    @app_commands.describe(mode="Configured verification mode, for example smoke or qa")
    async def verify_run(interaction: discord.Interaction, mode: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            run = await verification_service.enqueue_run(
                session_id=summary.id,
                mode=mode,
                requested_by=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            (
                f"Queued verification run `{run.id}` in mode `{run.mode}`.\n"
                f"Profile: `{run.profile_name}`\n"
                f"Artifacts: `{run.artifact_dir}`"
            ),
            ephemeral=True,
        )

    @verify_group.command(name="latest", description="Show the latest verification run for the current session")
    async def verify_latest(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            run = await verification_service.latest_run(session_id=summary.id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if run is None:
            await interaction.followup.send("No verification run has been recorded for this session yet.", ephemeral=True)
            return
        artifact_lines = "\n".join(
            f"- `{artifact.label}` [{artifact.artifact_type}] `{artifact.path}`"
            for artifact in run.artifacts[:8]
        ) or "- none"
        review_line = "none"
        if run.latest_review is not None:
            review_line = (
                f"`{run.latest_review.decision}` by `{run.latest_review.reviewer}`"
                f"{f' - {run.latest_review.note}' if run.latest_review.note else ''}"
            )
        await interaction.followup.send(
            (
                f"Verification `{run.id}`\n"
                f"- Status: `{run.status}`\n"
                f"- Mode: `{run.mode}`\n"
                f"- Profile: `{run.profile_name}`\n"
                f"- Summary: `{run.summary_text or run.error_text or 'n/a'}`\n"
                f"- Review: {review_line}\n"
                f"- Artifact dir: `{run.artifact_dir}`\n"
                f"Artifacts:\n{artifact_lines}"
            ),
            ephemeral=True,
        )

    @verify_group.command(name="approve", description="Approve the latest verification run that needs review")
    @app_commands.describe(note="Optional note for the review decision")
    async def verify_approve(interaction: discord.Interaction, note: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            run = await verification_service.review_latest(
                session_id=summary.id,
                decision="approved",
                reviewer=str(interaction.user.id),
                note=note,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Approved verification run `{run.id}` with status `{run.status}`.",
            ephemeral=True,
        )

    @verify_group.command(name="reject", description="Reject the latest verification run that needs review")
    @app_commands.describe(note="Optional reason or follow-up note")
    async def verify_reject(interaction: discord.Interaction, note: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _current_thread_session(interaction)
            run = await verification_service.review_latest(
                session_id=summary.id,
                decision="rejected",
                reviewer=str(interaction.user.id),
                note=note,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Rejected verification run `{run.id}` with status `{run.status}`.",
            ephemeral=True,
        )

    tree.add_command(project_group)
    tree.add_command(agent_group)
    tree.add_command(session_group)
    tree.add_command(policy_group)
    tree.add_command(verify_group)


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
