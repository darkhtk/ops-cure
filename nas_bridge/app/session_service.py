from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, selectinload

from .db import session_scope
from .drift_monitor import ArtifactSnapshot, DriftEvaluation, DriftMonitor
from .models import (
    AgentModel,
    ExecutionTargetModel,
    JobModel,
    PowerTargetModel,
    ProjectFindModel,
    SessionModel,
    SessionOperationModel,
    SessionPolicyModel,
    TranscriptModel,
)
from .schemas import (
    AgentStatusResponse,
    ArtifactHeartbeatSnapshot,
    ExecutionTargetSummary,
    JobPayload,
    PolicySetResponse,
    PowerTargetSummary,
    ProjectFindCandidate,
    ProjectFindCompleteRequest,
    ProjectFindLaunchResponse,
    ProjectManifest,
    SessionOperationResponse,
    SessionPauseResponse,
    SessionPolicyResponse,
    SessionLaunchResponse,
    ProjectFindSummaryResponse,
    SessionSummaryResponse,
    TranscriptContextEntry,
)
from .thread_manager import ThreadManager
from .transcript_service import TranscriptService, sanitize_text
from .worker_registry import WorkerRegistry

LOGGER = logging.getLogger(__name__)
AGENT_PREFIX_RE = re.compile(r"^\s*@(?P<agent>[A-Za-z0-9_-]+)\s+(?P<body>.+)$", re.DOTALL)
AGENT_THREAD_HEADER_RE = re.compile(r"^\s*\*\*(?P<agent>[A-Za-z0-9_-]+)(?:\s+error)?\*\*")
TASK_CARD_ID_RE = re.compile(r"\bT-\d{3}\b", re.IGNORECASE)
FOLLOW_UP_MESSAGE_RE = re.compile(
    r"^\s*(?:continue|go|proceed|keep going|ship it|do it|"
    r"계속|진행해|계속해|이어서|마저|해|해줘|해봐|처리해|다 해)\s*[.!?~]*\s*$",
    re.IGNORECASE,
)
FOLLOW_UP_HINT_RE = re.compile(
    r"(?:\b(?:continue|keep going|go on|resume|carry on|pick up(?: where you left off)?|ship it|do it)\b|"
    r"하던\s*(?:작업|거)|계속(?:해| 진행| 하)?|이어서|이어가|마저|처리해|다 해)",
    re.IGNORECASE,
)
HANDOFF_RE = re.compile(
    r"\[\[handoff\s+agent=(?P<quote>['\"]?)(?P<agent>[A-Za-z0-9_-]+)(?P=quote)\s*\]\]\s*"
    r"(?P<body>.*?)\s*\[\[/handoff\]\]",
    re.IGNORECASE | re.DOTALL,
)
RECENT_TRANSCRIPT_LIMIT = 12
RECENT_TRANSCRIPT_ENTRY_LIMIT = 800
HANDOFF_PREVIEW_LIMIT = 240
MAX_HANDOFFS_PER_COMPLETION = 4
SESSION_SUMMARY_INBOUND_LIMIT = 8
SESSION_SUMMARY_OUTBOUND_LIMIT = 6
SESSION_SUMMARY_SYSTEM_LIMIT = 6
SESSION_SUMMARY_PENDING_LIMIT = 6
SESSION_SUMMARY_TEXT_LIMIT = 220
ORPHANED_JOB_PREVIEW_LIMIT = 180
FAILURE_PREVIEW_LIMIT = 260
QUIET_DISCORD_CHAR_LIMIT = 520
QUIET_DISCORD_LINE_LIMIT = 7
MIN_HANDOFF_BODY_LENGTH = 40


@dataclass(slots=True)
class HandoffRequest:
    target_agent: str
    body: str
    task_id: str | None = None


@dataclass(slots=True)
class RoutingDecision:
    agent_name: str
    transcript_body: str
    job_input_text: str
    job_type: str = "message"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionService:
    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        thread_manager: ThreadManager,
        transcript_service: TranscriptService,
        drift_monitor: DriftMonitor,
    ) -> None:
        self.registry = registry
        self.thread_manager = thread_manager
        self.transcript_service = transcript_service
        self.drift_monitor = drift_monitor
        self.policy_service = None
        self.recovery_service = None
        self.start_workflow = None
        self.pause_workflow = None
        self.policy_workflow = None
        self.execution_provider = None

    def bind_orchestration(
        self,
        *,
        policy_service,
        recovery_service,
        start_workflow,
        pause_workflow,
        policy_workflow,
        execution_provider,
    ) -> None:
        self.policy_service = policy_service
        self.recovery_service = recovery_service
        self.start_workflow = start_workflow
        self.pause_workflow = pause_workflow
        self.policy_workflow = policy_workflow
        self.execution_provider = execution_provider

    async def create_session_from_project(
        self,
        *,
        project_name: str,
        target_project_name: str | None = None,
        preset: str | None,
        user_id: str,
        guild_id: str,
        parent_channel_id: str,
        workdir_override: str | None = None,
    ) -> SessionSummaryResponse:
        if self.start_workflow is not None:
            return await self.start_workflow.run(
                project_name=project_name,
                target_project_name=target_project_name,
                preset=preset,
                user_id=user_id,
                guild_id=guild_id,
                parent_channel_id=parent_channel_id,
                workdir_override=workdir_override,
            )
        manifest, selected_preset = self._resolve_manifest(preset)
        self._validate_session_start(
            manifest,
            user_id=user_id,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
        )

        return await self._create_session_with_manifest(
            project_name=project_name,
            target_project_name=target_project_name or project_name,
            selected_preset=selected_preset,
            manifest=manifest,
            user_id=user_id,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            workdir_override=workdir_override,
        )

    async def pause_session(
        self,
        *,
        session_id: str,
        requested_by: str,
        reason: str | None = None,
    ) -> SessionPauseResponse:
        if self.pause_workflow is None:
            raise RuntimeError("Pause workflow is not configured.")
        return await self.pause_workflow.pause(
            session_id=session_id,
            requested_by=requested_by,
            reason=reason,
        )

    async def resume_session(
        self,
        *,
        session_id: str,
        requested_by: str,
    ) -> SessionPauseResponse:
        if self.pause_workflow is None:
            raise RuntimeError("Pause workflow is not configured.")
        return await self.pause_workflow.resume(
            session_id=session_id,
            requested_by=requested_by,
        )

    async def show_policy(self, *, session_id: str) -> SessionPolicyResponse:
        if self.policy_workflow is None:
            raise RuntimeError("Policy workflow is not configured.")
        return await self.policy_workflow.show(session_id=session_id)

    async def set_policy(
        self,
        *,
        session_id: str,
        key: str,
        value: str,
        updated_by: str,
    ) -> PolicySetResponse:
        if self.policy_workflow is None:
            raise RuntimeError("Policy workflow is not configured.")
        return await self.policy_workflow.set(
            session_id=session_id,
            key=key,
            value=value,
            updated_by=updated_by,
        )

    async def _create_session_with_manifest(
        self,
        *,
        project_name: str,
        target_project_name: str,
        selected_preset: str,
        manifest: ProjectManifest,
        user_id: str,
        guild_id: str,
        parent_channel_id: str,
        workdir_override: str | None = None,
    ) -> SessionSummaryResponse:
        resolved_workdir = (workdir_override or manifest.default_workdir).strip()
        if not resolved_workdir:
            raise ValueError("Resolved workdir cannot be empty.")

        discord_thread_id = await self.thread_manager.create_session_thread(
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            project_name=project_name,
            template=manifest.discord.thread_name_template,
            auto_archive_duration=manifest.discord.auto_archive_duration,
        )

        summary: SessionSummaryResponse
        with session_scope() as db:
            session_row = SessionModel(
                project_name=project_name,
                target_project_name=target_project_name,
                preset=selected_preset,
                discord_thread_id=discord_thread_id,
                guild_id=manifest.guild_id,
                parent_channel_id=manifest.parent_channel_id,
                workdir=resolved_workdir,
                status="requested",
                desired_status="ready",
                power_state="unknown",
                execution_state="unknown",
                created_by=user_id,
                send_ready_message=manifest.startup.send_ready_message,
            )
            db.add(session_row)
            db.flush()

            for agent in manifest.agents:
                db.add(
                    AgentModel(
                        session_id=session_row.id,
                        agent_name=agent.name,
                        cli_type=agent.cli,
                        role=agent.role,
                        is_default=agent.default,
                        status="starting",
                        desired_status="ready",
                    ),
                )

            db.flush()
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(
                    selectinload(SessionModel.agents),
                    selectinload(SessionModel.policies),
                    selectinload(SessionModel.operations),
                )
                .where(SessionModel.id == session_row.id),
            )
            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor="bridge",
                content=(
                    f"Session created with title {project_name} targeting {target_project_name} "
                f"using profile {selected_preset}."
                    f" Workdir: {resolved_workdir}"
                ),
            )
            summary = self._to_summary_response(db, session_row)

        await self.thread_manager.post_message(
            discord_thread_id,
            (
                f"Session `{summary.id}` created for `{project_name}` using profile `{selected_preset}`.\n"
                "Launcher claim is pending. Workers will join this thread when ready."
            ),
        )
        return summary

    async def claim_launches(self, launcher_id: str, capacity: int) -> list[SessionLaunchResponse]:
        manifests = self.registry.get_projects_for_launcher(launcher_id)
        if not manifests:
            return []

        launches: list[SessionLaunchResponse] = []
        preset_names = list(manifests.keys())

        with session_scope() as db:
            statement = (
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.preset.in_(preset_names))
                .where(SessionModel.status.in_(["requested", "waiting_for_workers", "launching", "awaiting_launcher", "restarting_workers"]))
                .where(SessionModel.desired_status != "paused")
                .where(SessionModel.closed_at.is_(None))
                .order_by(SessionModel.created_at.asc())
            )
            sessions = list(db.scalars(statement))
            for session_row in sessions:
                if len(launches) >= capacity:
                    break
                manifest = manifests.get(session_row.preset or "")
                if manifest is None:
                    continue
                if session_row.status == "launching" and not manifest.startup.restore_last_session:
                    continue
                session_row.status = "launching"
                session_row.execution_state = "launching"
                session_row.launcher_id = launcher_id
                launches.append(self._to_launch_response(session_row))

        return launches

    async def enqueue_project_find(
        self,
        *,
        query_text: str,
        preset: str | None,
        user_id: str,
        guild_id: str,
        parent_channel_id: str,
    ) -> ProjectFindSummaryResponse:
        manifest, selected_preset = self._resolve_manifest(preset)
        self._validate_session_start(
            manifest,
            user_id=user_id,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
        )
        if not manifest.finder.roots:
            raise ValueError(f"Profile `{selected_preset}` does not have any configured finder roots.")

        with session_scope() as db:
            find_row = ProjectFindModel(
                preset=selected_preset,
                query_text=query_text.strip(),
                requested_by=user_id,
                guild_id=guild_id,
                parent_channel_id=parent_channel_id,
                status="pending",
            )
            db.add(find_row)
            db.flush()
            return self._to_project_find_response(find_row)

    async def claim_project_finds(self, launcher_id: str, capacity: int) -> list[ProjectFindLaunchResponse]:
        manifests = self.registry.get_projects_for_launcher(launcher_id)
        if not manifests:
            return []

        launches: list[ProjectFindLaunchResponse] = []
        preset_names = [
            preset_name
            for preset_name, manifest in manifests.items()
            if manifest.finder.roots
        ]
        if not preset_names:
            return launches

        with session_scope() as db:
            rows = list(
                db.scalars(
                    select(ProjectFindModel)
                    .where(ProjectFindModel.preset.in_(preset_names))
                    .where(ProjectFindModel.status == "pending")
                    .order_by(ProjectFindModel.created_at.asc())
                    .limit(capacity),
                ),
            )
            for row in rows:
                manifest = manifests.get(row.preset)
                if manifest is None:
                    continue
                row.status = "claimed"
                row.launcher_id = launcher_id
                row.claimed_at = utcnow()
                launches.append(
                    ProjectFindLaunchResponse(
                        id=row.id,
                        preset=row.preset,
                        query_text=row.query_text,
                        requested_by=row.requested_by,
                        guild_id=row.guild_id,
                        parent_channel_id=row.parent_channel_id,
                        finder=manifest.finder,
                        created_at=row.created_at,
                    ),
                )
        return launches

    async def complete_project_find(
        self,
        *,
        find_id: str,
        launcher_id: str,
        status: str,
        selected_path: str | None,
        selected_name: str | None,
        reason: str | None,
        confidence: float | None,
        candidates: list[ProjectFindCandidate],
        error_text: str | None,
    ) -> ProjectFindSummaryResponse:
        with session_scope() as db:
            row = self._require_project_find(db, find_id=find_id)
            if row.launcher_id not in (None, launcher_id):
                raise PermissionError("Project find request is owned by another launcher.")

            row.launcher_id = launcher_id
            row.status = status
            row.selected_path = selected_path
            row.selected_name = selected_name
            row.reason = reason
            row.confidence = confidence
            row.error_text = error_text
            row.candidates_json = json.dumps([candidate.model_dump() for candidate in candidates], ensure_ascii=False)
            row.completed_at = utcnow()
            return self._to_project_find_response(row)

    async def get_project_find(self, find_id: str) -> ProjectFindSummaryResponse:
        with session_scope() as db:
            row = self._require_project_find(db, find_id=find_id)
            return self._to_project_find_response(row)

    async def wait_for_project_find(
        self,
        *,
        find_id: str,
        timeout_seconds: int = 75,
        poll_interval_seconds: float = 1.5,
    ) -> ProjectFindSummaryResponse | None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        terminal_states = {"selected", "needs_clarification", "no_match", "failed", "started"}
        while asyncio.get_running_loop().time() < deadline:
            summary = await self.get_project_find(find_id)
            if summary.status in terminal_states:
                return summary
            await asyncio.sleep(poll_interval_seconds)
        return None

    async def mark_project_find_started(
        self,
        *,
        find_id: str,
        session_summary: SessionSummaryResponse,
    ) -> ProjectFindSummaryResponse:
        with session_scope() as db:
            row = self._require_project_find(db, find_id=find_id)
            row.status = "started"
            row.session_id = session_summary.id
            row.discord_thread_id = session_summary.discord_thread_id
            row.completed_at = row.completed_at or utcnow()
            return self._to_project_find_response(row)

    async def get_session_exists_by_thread_id(self, thread_id: str) -> bool:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel.id).where(SessionModel.discord_thread_id == thread_id),
            )
            return session_row is not None

    async def get_session_summary(self, session_id: str) -> SessionSummaryResponse:
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(
                    selectinload(SessionModel.agents),
                    selectinload(SessionModel.policies),
                    selectinload(SessionModel.operations),
                )
                .where(SessionModel.id == session_id),
            )
            return self._to_summary_response(db, session_row)

    async def get_session_summary_by_thread(self, thread_id: str) -> SessionSummaryResponse:
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(
                    selectinload(SessionModel.agents),
                    selectinload(SessionModel.policies),
                    selectinload(SessionModel.operations),
                )
                .where(SessionModel.discord_thread_id == thread_id),
            )
            return self._to_summary_response(db, session_row)

    async def close_session(self, thread_id: str, closed_by: str) -> SessionSummaryResponse:
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.discord_thread_id == thread_id),
            )
            session_row.status = "closed"
            session_row.desired_status = "closed"
            session_row.closed_at = utcnow()

            for agent in session_row.agents:
                agent.status = "offline"

            for job in db.scalars(
                select(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status.in_(["pending", "in_progress"])),
            ):
                job.status = "cancelled"
                job.completed_at = utcnow()
                job.error_text = "Session closed."

            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor=closed_by,
                content="Session closed from Discord.",
            )
            summary = self._to_summary_response(db, session_row)

        self.drift_monitor.clear_session(summary.id)
        await self.thread_manager.post_message(thread_id, "Session closed. New jobs will be rejected.")
        await self.thread_manager.archive_thread(thread_id, "Session closed")
        return summary

    async def enqueue_restart(self, session_id: str, agent_name: str, requested_by: str) -> JobPayload:
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel).where(SessionModel.id == session_id),
            )
            agent = db.scalar(
                select(AgentModel)
                .where(AgentModel.session_id == session_id)
                .where(AgentModel.agent_name == agent_name),
            )
            if agent is None:
                raise ValueError(f"Agent '{agent_name}' does not exist in this session.")

            job = JobModel(
                session_id=session_row.id,
                agent_name=agent_name,
                job_type="restart",
                user_id=requested_by,
                input_text=f"Restart requested by {requested_by}",
                status="pending",
            )
            db.add(job)
            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor=requested_by,
                content=f"Restart requested for agent {agent_name}.",
            )
            db.flush()
            db.refresh(job)
            session_with_agents = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_row.id),
            )
            recent_transcript = self._load_recent_transcript(db, session_id=session_row.id)
            session_summary = self._build_session_summary(
                db,
                session_row=session_with_agents,
                current_agent=agent_name,
            )
            return self._to_job_payload(
                job,
                session_row=session_with_agents,
                session_summary=session_summary,
                recent_transcript=recent_transcript,
            )

    async def reset_session(self, session_id: str, requested_by: str) -> None:
        thread_id = ""
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            thread_id = session_row.discord_thread_id

            for job in db.scalars(
                select(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status == "pending"),
            ):
                job.status = "cancelled"
                job.completed_at = utcnow()
                job.error_text = "Cancelled by session reset."

            for agent in session_row.agents:
                db.add(
                    JobModel(
                        session_id=session_row.id,
                        agent_name=agent.agent_name,
                        job_type="restart",
                        user_id=requested_by,
                        input_text=f"Reset requested by {requested_by}",
                        status="pending",
                    ),
                )

            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor=requested_by,
                content="Session reset requested.",
            )

        await self.thread_manager.post_message(
            thread_id,
            "Session reset requested. Pending jobs were cleared and all agents will restart.",
        )

    async def route_discord_message(
        self,
        *,
        thread_id: str,
        discord_message_id: str,
        user_id: str,
        content: str,
        author_name: str,
        reply_message_id: str | None = None,
        reply_content: str | None = None,
    ) -> str:
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.discord_thread_id == thread_id),
            )
            if session_row.status == "closed":
                raise ValueError("This session is already closed.")
            if session_row.desired_status == "paused" or session_row.status == "paused":
                raise ValueError("This session is paused. Resume it before sending new work.")

            routing = self._resolve_agent(
                db,
                session_row=session_row,
                content=content,
                reply_message_id=reply_message_id,
                reply_content=reply_content,
            )
            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="inbound",
                actor=author_name,
                content=routing.transcript_body,
                source_discord_message_id=discord_message_id,
            )
            if routing.job_type != "message":
                routing_label = {
                    "orchestration": "Planner-first orchestration",
                    "routing": "LLM-assisted routing",
                    "handoff_repair": "Planner handoff repair",
                }.get(routing.job_type, "Planner coordination")
                self.transcript_service.add_entry(
                    db,
                    session_id=session_row.id,
                    direction="system",
                    actor="bridge",
                    content=(
                        f"{routing_label} routed the request to {routing.agent_name} "
                        f"as `{routing.job_type}` work."
                    ),
                    source_discord_message_id=discord_message_id,
                )
            db.add(
                JobModel(
                    session_id=session_row.id,
                    agent_name=routing.agent_name,
                    job_type=routing.job_type,
                    source_discord_message_id=discord_message_id,
                    user_id=user_id,
                    input_text=routing.job_input_text,
                    status="pending",
                ),
            )
            LOGGER.info(
                "Queued job for session=%s agent=%s type=%s",
                session_row.id,
                routing.agent_name,
                routing.job_type,
            )
            return routing.agent_name

    async def register_worker(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
        pid_hint: int | None,
    ) -> None:
        should_send_ready = False
        ready_message = ""
        thread_id = ""

        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            previous_status = session_row.status
            agent = self._require_agent(db, session_id=session_id, agent_name=agent_name)
            agent.worker_id = worker_id
            agent.pid_hint = pid_hint
            agent.status = "paused" if session_row.desired_status == "paused" else "idle"
            agent.last_heartbeat_at = utcnow()
            agent.last_error = None
            session_row.execution_state = "online"
            session_row.status = "paused" if session_row.desired_status == "paused" else "launching"
            thread_id = session_row.discord_thread_id

            if all(member.worker_id for member in session_row.agents):
                session_row.status = "paused" if session_row.desired_status == "paused" else "ready"
                if previous_status != "ready":
                    should_send_ready = session_row.send_ready_message and session_row.desired_status != "paused"
                    ready_message = self._format_ready_message(session_row.agents)

            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor=agent_name,
                content=f"Worker {worker_id} registered for agent {agent_name}.",
            )

        self.drift_monitor.register_worker(
            session_id=session_id,
            agent_name=agent_name,
            worker_id=worker_id,
            worker_status="idle",
        )
        if should_send_ready:
            await self.thread_manager.post_message(thread_id, ready_message)

    async def heartbeat(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
        status: str,
        pid_hint: int | None,
        artifact_snapshot: ArtifactHeartbeatSnapshot | None = None,
    ) -> None:
        with session_scope() as db:
            agent = self._require_agent(db, session_id=session_id, agent_name=agent_name)
            agent.worker_id = worker_id
            agent.status = "paused" if agent.desired_status == "paused" and status != "busy" else status
            agent.pid_hint = pid_hint
            agent.last_heartbeat_at = utcnow()
        self.drift_monitor.record_heartbeat(
            session_id=session_id,
            agent_name=agent_name,
            worker_id=worker_id,
            worker_status=status,
            artifact_snapshot=self._to_artifact_snapshot(artifact_snapshot),
        )

    async def claim_next_job(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
    ) -> JobPayload | None:
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            if session_row.status == "closed":
                return None
            if session_row.desired_status == "paused" or session_row.status in {"paused", "awaiting_launcher", "waking_execution_plane"}:
                agent = self._require_agent(db, session_id=session_id, agent_name=agent_name)
                if session_row.desired_status == "paused":
                    agent.status = "paused"
                return None

            agent = self._require_agent(db, session_id=session_id, agent_name=agent_name)
            policy = self._load_policy_summary(db, session_row)
            max_parallel_agents = policy.max_parallel_agents if policy is not None else max(1, len(session_row.agents))

            active_job = db.scalar(
                select(JobModel)
                .where(JobModel.session_id == session_id)
                .where(JobModel.agent_name == agent_name)
                .where(JobModel.status == "in_progress"),
            )
            if active_job is not None:
                recovered = self._recover_orphaned_job(
                    db,
                    session_id=session_id,
                    agent=agent,
                    active_job=active_job,
                    worker_id=worker_id,
                )
                if recovered:
                    active_job = None
                else:
                    agent.status = "busy"
                    return None

            if active_job is not None:
                agent.status = "busy"
                return None

            active_job_count = int(
                db.scalar(
                    select(func.count())
                    .select_from(JobModel)
                    .where(JobModel.session_id == session_id)
                    .where(JobModel.status == "in_progress"),
                )
                or 0,
            )
            if active_job_count >= max_parallel_agents:
                agent.status = "idle"
                return None

            job = db.scalar(
                select(JobModel)
                .where(JobModel.session_id == session_id)
                .where(JobModel.agent_name == agent_name)
                .where(JobModel.status == "pending")
                .order_by(JobModel.created_at.asc()),
            )
            if job is None:
                agent.status = "idle"
                return None

            job.status = "in_progress"
            job.claimed_at = utcnow()
            job.worker_id = worker_id
            agent.status = "busy"
            db.flush()
            db.refresh(job)
            recent_transcript = self._load_recent_transcript(db, session_id=session_id)
            session_summary = self._build_session_summary(
                db,
                session_row=session_row,
                current_agent=agent_name,
            )
            return self._to_job_payload(
                job,
                session_row=session_row,
                session_summary=session_summary,
                recent_transcript=recent_transcript,
            )

    async def complete_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        output_text: str,
        pid_hint: int | None,
    ) -> None:
        sanitized = sanitize_text(output_text)
        thread_id = ""
        thread_message = ""
        visible_output = ""
        quiet_discord = True
        with session_scope() as db:
            job = self._require_job(
                db,
                job_id=job_id,
                session_id=session_id,
                agent_name=agent_name,
                worker_id=worker_id,
            )
            agent = self._require_agent(db, session_id=session_id, agent_name=agent_name)
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            policy = self._load_policy_summary(db, session_row)
            quiet_discord = policy.quiet_discord if policy is not None else True
            visible_output, handoffs, rejected_handoffs = self._extract_handoffs(
                sanitized,
                agents=session_row.agents,
                source_agent=agent_name,
            )

            job.status = "completed"
            job.result_text = visible_output
            job.completed_at = utcnow()

            agent.status = "paused" if session_row.desired_status == "paused" else "idle"
            agent.pid_hint = pid_hint
            agent.last_heartbeat_at = utcnow()
            agent.last_error = None
            thread_id = session_row.discord_thread_id

            self._queue_handoffs(
                db,
                session_id=session_id,
                source_agent=agent_name,
                source_job=job,
                handoffs=handoffs,
            )
            if rejected_handoffs:
                self._record_handoff_rejections(
                    db,
                    session_row=session_row,
                    source_agent=agent_name,
                    source_job=job,
                    rejections=rejected_handoffs,
                )
            thread_message = self._format_agent_thread_message(
                agent_name=agent_name,
                visible_output=visible_output,
                handoffs=handoffs,
                quiet_discord=quiet_discord,
            )

        if thread_message:
            sent_chunks = await self.thread_manager.post_message(thread_id, thread_message)
            if visible_output:
                self._record_outbound_thread_messages(
                    session_id=session_id,
                    agent_name=agent_name,
                    sent_chunks=sent_chunks,
                )

    async def fail_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        error_text: str,
        pid_hint: int | None,
    ) -> None:
        sanitized = sanitize_text(error_text)
        thread_id = ""
        thread_message = ""
        quiet_discord = True
        with session_scope() as db:
            job = self._require_job(
                db,
                job_id=job_id,
                session_id=session_id,
                agent_name=agent_name,
                worker_id=worker_id,
            )
            agent = self._require_agent(db, session_id=session_id, agent_name=agent_name)
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            policy = self._load_policy_summary(db, session_row)
            quiet_discord = policy.quiet_discord if policy is not None else True

            job.status = "failed"
            job.error_text = sanitized
            job.completed_at = utcnow()

            agent.status = "paused" if session_row.desired_status == "paused" else "idle"
            agent.pid_hint = pid_hint
            agent.last_heartbeat_at = utcnow()
            agent.last_error = sanitized
            thread_id = session_row.discord_thread_id

            recovery_queued = self._queue_planner_recovery(
                db,
                session_row=session_row,
                failed_job=job,
                failed_agent=agent_name,
                sanitized_error=sanitized,
            )
            thread_message = self._format_failure_thread_message(
                agent_name=agent_name,
                sanitized_error=sanitized,
                recovery_queued=recovery_queued,
                quiet_discord=quiet_discord,
            )

        sent_chunks = await self.thread_manager.post_message(thread_id, thread_message)
        self._record_outbound_thread_messages(
            session_id=session_id,
            agent_name=agent_name,
            sent_chunks=sent_chunks,
        )

    def _resolve_agent(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        content: str,
        reply_message_id: str | None,
        reply_content: str | None,
    ) -> RoutingDecision:
        agent_list = list(session_row.agents)
        body = content.strip()
        match = AGENT_PREFIX_RE.match(content)
        if match:
            requested_name = match.group("agent")
            body = match.group("body").strip()
            for agent in agent_list:
                if agent.agent_name.lower() == requested_name.lower():
                    return RoutingDecision(
                        agent_name=agent.agent_name,
                        transcript_body=body,
                        job_input_text=body,
                    )
            raise ValueError(f"Unknown agent '{requested_name}'.")

        if len(agent_list) == 1:
            return RoutingDecision(
                agent_name=agent_list[0].agent_name,
                transcript_body=body,
                job_input_text=body,
            )

        reply_agent = self._find_reply_agent(
            db,
            session_id=session_row.id,
            agents=agent_list,
            reply_message_id=reply_message_id,
            reply_content=reply_content,
        )
        if reply_agent is not None:
            return RoutingDecision(
                agent_name=reply_agent,
                transcript_body=body,
                job_input_text=body,
            )

        planner_agent = self._find_planner_agent(agent_list)
        last_active_agent = self._find_last_active_agent(
            db,
            session_id=session_row.id,
            agents=agent_list,
        )
        default_agent = self._find_default_agent(agent_list)
        if self._should_route_to_planner_first(
            content=body,
            planner_agent=planner_agent,
        ):
            assert planner_agent is not None
            return RoutingDecision(
                agent_name=planner_agent.agent_name,
                transcript_body=body,
                job_input_text=self._build_planner_orchestration_prompt(body),
                job_type="orchestration",
            )

        if planner_agent is not None:
            return RoutingDecision(
                agent_name=planner_agent.agent_name,
                transcript_body=body,
                job_input_text=self._build_planner_routing_prompt(
                    user_text=body,
                    last_active_agent=last_active_agent,
                    default_agent=default_agent,
                ),
                job_type="routing",
            )

        if last_active_agent is not None:
            return RoutingDecision(agent_name=last_active_agent, transcript_body=body, job_input_text=body)

        if default_agent is not None:
            return RoutingDecision(agent_name=default_agent, transcript_body=body, job_input_text=body)

        available = ", ".join(f"@{agent.agent_name}" for agent in agent_list)
        raise ValueError(
            "Multiple agents are available and no routing context was found. "
            f"Reply to an agent message or prefix with one of: {available}",
        )

    def _format_ready_message(self, agents: list[AgentModel]) -> str:
        sorted_agents = sorted(agents, key=lambda value: value.agent_name)
        status_lines = []
        for agent in sorted_agents:
            default_marker = " (default)" if agent.is_default else ""
            status_lines.append(
                f"- `{agent.agent_name}` [{agent.cli_type}] {agent.role}{default_marker}",
            )

        if len(sorted_agents) == 1:
            routing_hint = (
                "Workers are ready. Send your next message directly in this thread and it will "
                f"route to `{sorted_agents[0].agent_name}` automatically."
            )
        else:
            mentions = ", ".join(f"`@{agent.agent_name}`" for agent in sorted_agents)
            routing_hint = (
                "Workers are ready. Plain text without a reply or explicit agent tag is first interpreted by "
                "the local planner for LLM-assisted routing. Reply to an agent message to continue directly "
                f"with that agent, or use {mentions} when you want to override routing."
            )

        return routing_hint + "\n" + "\n".join(status_lines)

    def _find_reply_agent(
        self,
        db: Session,
        *,
        session_id: str,
        agents: Iterable[AgentModel],
        reply_message_id: str | None,
        reply_content: str | None,
    ) -> str | None:
        if not reply_message_id:
            return self._extract_agent_from_thread_message(reply_content, agents)

        agent_names = {agent.agent_name.lower(): agent.agent_name for agent in agents}
        entry = db.scalar(
            select(TranscriptModel)
            .where(TranscriptModel.session_id == session_id)
            .where(TranscriptModel.direction == "outbound")
            .where(TranscriptModel.source_discord_message_id == reply_message_id)
            .order_by(desc(TranscriptModel.created_at)),
        )
        if entry is not None:
            mapped_agent = agent_names.get(entry.actor.lower())
            if mapped_agent is not None:
                return mapped_agent
        return self._extract_agent_from_thread_message(reply_content, agents)

    def _find_last_active_agent(
        self,
        db: Session,
        *,
        session_id: str,
        agents: Iterable[AgentModel],
    ) -> str | None:
        agent_names = {agent.agent_name.lower(): agent.agent_name for agent in agents}
        recent_entry = db.scalar(
            select(TranscriptModel)
            .where(TranscriptModel.session_id == session_id)
            .where(TranscriptModel.direction == "outbound")
            .order_by(desc(TranscriptModel.created_at)),
        )
        if recent_entry is None:
            return None
        return agent_names.get(recent_entry.actor.lower())

    @staticmethod
    def _find_default_agent(agents: Iterable[AgentModel]) -> str | None:
        for agent in agents:
            if agent.is_default:
                return agent.agent_name
        return None

    @staticmethod
    def _extract_agent_from_thread_message(
        content: str | None,
        agents: Iterable[AgentModel],
    ) -> str | None:
        if not content:
            return None
        match = AGENT_THREAD_HEADER_RE.match(content.strip())
        if not match:
            return None
        requested_name = match.group("agent")
        for agent in agents:
            if agent.agent_name.lower() == requested_name.lower():
                return agent.agent_name
        return None

    def _to_launch_response(self, session_row: SessionModel) -> SessionLaunchResponse:
        return SessionLaunchResponse(
            session_id=session_row.id,
            project_name=session_row.project_name,
            target_project_name=session_row.target_project_name,
            preset=session_row.preset or "",
            workdir=session_row.workdir,
            status=session_row.status,
            agents=[self._to_agent_response(agent) for agent in session_row.agents],
        )

    def _to_summary_response(self, db: Session, session_row: SessionModel) -> SessionSummaryResponse:
        return SessionSummaryResponse(
            id=session_row.id,
            project_name=session_row.project_name,
            target_project_name=session_row.target_project_name,
            preset=session_row.preset,
            discord_thread_id=session_row.discord_thread_id,
            guild_id=session_row.guild_id,
            parent_channel_id=session_row.parent_channel_id,
            workdir=session_row.workdir,
            status=session_row.status,
            desired_status=session_row.desired_status,
            power_state=session_row.power_state,
            execution_state=session_row.execution_state,
            pause_reason=session_row.pause_reason,
            last_recovery_at=session_row.last_recovery_at,
            last_recovery_reason=session_row.last_recovery_reason,
            created_by=session_row.created_by,
            launcher_id=session_row.launcher_id,
            created_at=session_row.created_at,
            closed_at=session_row.closed_at,
            power_target=self._load_power_target_summary(db, session_row),
            execution_target=self._load_execution_target_summary(db, session_row),
            policy=self._load_policy_summary(db, session_row),
            active_operation=self._load_active_operation(db, session_row),
            agents=[self._to_agent_response(agent) for agent in session_row.agents],
        )

    def _load_power_target_summary(self, db: Session, session_row: SessionModel) -> PowerTargetSummary | None:
        if not session_row.power_target_name:
            return None
        row = db.scalar(
            select(PowerTargetModel).where(PowerTargetModel.name == session_row.power_target_name),
        )
        if row is None:
            return None
        return PowerTargetSummary(
            name=row.name,
            provider=row.provider,
            state=session_row.power_state,
        )

    def _load_execution_target_summary(
        self,
        db: Session,
        session_row: SessionModel,
    ) -> ExecutionTargetSummary | None:
        if not session_row.execution_target_name:
            return None
        row = db.scalar(
            select(ExecutionTargetModel).where(ExecutionTargetModel.name == session_row.execution_target_name),
        )
        if row is None:
            return None
        metadata = {}
        if row.metadata_json:
            metadata = json.loads(row.metadata_json)
        return ExecutionTargetSummary(
            name=row.name,
            provider=row.provider,
            platform=row.platform,
            state=session_row.execution_state,
            launcher_id=session_row.launcher_id,
            auto_start_expected=bool(metadata.get("auto_start_expected", True)),
        )

    def _load_policy_summary(self, db: Session, session_row: SessionModel) -> SessionPolicyResponse | None:
        if self.policy_service is None or session_row.preset is None:
            return None
        manifest = self.registry.get_project(session_row.preset)
        return self.policy_service.get_policy_response(
            db,
            session_id=session_row.id,
            manifest=manifest,
        )

    @staticmethod
    def _load_active_operation(db: Session, session_row: SessionModel) -> SessionOperationResponse | None:
        operation = db.scalar(
            select(SessionOperationModel)
            .where(SessionOperationModel.session_id == session_row.id)
            .where(SessionOperationModel.status.in_(["pending", "running"]))
            .order_by(SessionOperationModel.created_at.desc()),
        )
        if operation is None:
            return None
        return SessionOperationResponse(
            id=operation.id,
            operation_type=operation.operation_type,
            status=operation.status,
            requested_by=operation.requested_by,
            created_at=operation.created_at,
            completed_at=operation.completed_at,
        )

    def _to_project_find_response(self, row: ProjectFindModel) -> ProjectFindSummaryResponse:
        candidates: list[ProjectFindCandidate] = []
        if row.candidates_json:
            try:
                raw_candidates = json.loads(row.candidates_json)
                candidates = [ProjectFindCandidate.model_validate(item) for item in raw_candidates]
            except Exception:  # noqa: BLE001
                LOGGER.warning("Failed to decode stored project find candidates for %s", row.id)
        return ProjectFindSummaryResponse(
            id=row.id,
            preset=row.preset,
            query_text=row.query_text,
            status=row.status,
            requested_by=row.requested_by,
            guild_id=row.guild_id,
            parent_channel_id=row.parent_channel_id,
            launcher_id=row.launcher_id,
            selected_path=row.selected_path,
            selected_name=row.selected_name,
            reason=row.reason,
            confidence=row.confidence,
            candidates=candidates,
            error_text=row.error_text,
            session_id=row.session_id,
            discord_thread_id=row.discord_thread_id,
            created_at=row.created_at,
            claimed_at=row.claimed_at,
            completed_at=row.completed_at,
        )

    def _resolve_manifest(self, preset: str | None) -> tuple[ProjectManifest, str]:
        available_presets = self.registry.list_project_names()
        if preset:
            manifest = self.registry.get_project(preset)
            if manifest is None:
                available = ", ".join(sorted(available_presets)) or "(none)"
                raise ValueError(
                    f"Profile '{preset}' is not currently registered by any launcher. Available profiles: {available}",
                )
            return manifest, preset

        if len(available_presets) == 1:
            selected_preset = available_presets[0]
            manifest = self.registry.get_project(selected_preset)
            assert manifest is not None
            return manifest, selected_preset

        if "sample" in available_presets:
            manifest = self.registry.get_project("sample")
            assert manifest is not None
            return manifest, "sample"

        available = ", ".join(sorted(available_presets)) or "(none)"
        raise ValueError(
            f"Multiple profiles are registered. Please provide /project start profile:<name>. Available profiles: {available}",
        )

    def _to_agent_response(self, agent: AgentModel) -> AgentStatusResponse:
        drift = self._evaluate_agent_drift(agent)
        return AgentStatusResponse(
            agent_name=agent.agent_name,
            cli_type=agent.cli_type,
            role=agent.role,
            is_default=agent.is_default,
            status=agent.status,
            desired_status=agent.desired_status,
            paused_reason=agent.paused_reason,
            last_heartbeat_at=agent.last_heartbeat_at,
            worker_id=agent.worker_id,
            pid_hint=agent.pid_hint,
            drift_state=drift.drift_state,
            drift_reason=drift.drift_reason,
            workspace_ready=drift.workspace_ready,
            last_artifact_at=drift.last_artifact_at,
            last_artifact_path=drift.last_artifact_path,
            current_task_id=drift.current_task_id,
            current_task_state=drift.current_task_state,
        )

    def get_drift_overview(self) -> tuple[int, int]:
        with session_scope() as db:
            sessions = list(
                db.scalars(
                    select(SessionModel)
                    .options(selectinload(SessionModel.agents))
                    .where(SessionModel.closed_at.is_(None)),
                ),
            )
            agents_in_drift = 0
            sessions_with_drift = 0
            for session_row in sessions:
                session_has_drift = False
                for agent in session_row.agents:
                    if self._evaluate_agent_drift(agent).drift_state != "drift":
                        continue
                    agents_in_drift += 1
                    session_has_drift = True
                if session_has_drift:
                    sessions_with_drift += 1
            return agents_in_drift, sessions_with_drift

    def _evaluate_agent_drift(self, agent: AgentModel) -> DriftEvaluation:
        return self.drift_monitor.evaluate_agent(
            session_id=agent.session_id,
            agent_name=agent.agent_name,
            agent_status=agent.status,
            worker_id=agent.worker_id,
        )

    @staticmethod
    def _to_artifact_snapshot(payload: ArtifactHeartbeatSnapshot | None) -> ArtifactSnapshot | None:
        if payload is None:
            return None
        return ArtifactSnapshot(
            workspace_ready=payload.workspace_ready,
            state_label=payload.state_label,
            state_updated_at=payload.state_updated_at,
            current_task_state=payload.current_task_state,
            current_task_id=payload.current_task_id,
            current_task_updated_at=payload.current_task_updated_at,
            latest_artifact_at=payload.latest_artifact_at,
            latest_artifact_path=payload.latest_artifact_path,
        )

    def _to_job_payload(
        self,
        job: JobModel,
        *,
        session_row: SessionModel,
        session_summary: str,
        recent_transcript: list[TranscriptContextEntry],
    ) -> JobPayload:
        return JobPayload(
            id=job.id,
            session_id=job.session_id,
            agent_name=job.agent_name,
            job_type=job.job_type,
            input_text=job.input_text,
            user_id=job.user_id,
            project_name=session_row.target_project_name or session_row.project_name,
            session_title=session_row.project_name,
            target_project_name=session_row.target_project_name,
            preset=session_row.preset,
            session_status=session_row.status,
            session_summary=session_summary,
            available_agents=[self._to_agent_response(agent) for agent in session_row.agents],
            recent_transcript=recent_transcript,
            source_discord_message_id=job.source_discord_message_id,
            created_at=job.created_at,
        )

    def _load_recent_transcript(
        self,
        db: Session,
        *,
        session_id: str,
        limit: int = RECENT_TRANSCRIPT_LIMIT,
    ) -> list[TranscriptContextEntry]:
        entries = list(
            db.scalars(
                select(TranscriptModel)
                .where(TranscriptModel.session_id == session_id)
                .order_by(desc(TranscriptModel.created_at))
                .limit(limit),
            ),
        )
        entries.reverse()
        return [
            TranscriptContextEntry(
                direction=entry.direction,
                actor=entry.actor,
                content=self._trim_context_text(entry.content, RECENT_TRANSCRIPT_ENTRY_LIMIT),
                created_at=entry.created_at,
            )
            for entry in entries
        ]

    def _build_session_summary(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        current_agent: str,
    ) -> str:
        inbound_entries = self._load_transcript_direction(
            db,
            session_id=session_row.id,
            direction="inbound",
            limit=SESSION_SUMMARY_INBOUND_LIMIT,
        )
        outbound_entries = self._load_transcript_direction(
            db,
            session_id=session_row.id,
            direction="outbound",
            limit=SESSION_SUMMARY_OUTBOUND_LIMIT,
        )
        system_entries = [
            entry
            for entry in self._load_transcript_direction(
                db,
                session_id=session_row.id,
                direction="system",
                limit=SESSION_SUMMARY_SYSTEM_LIMIT * 2,
            )
            if not entry.content.startswith("Worker ")
        ][:SESSION_SUMMARY_SYSTEM_LIMIT]
        pending_jobs = list(
            db.scalars(
                select(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status.in_(["pending", "in_progress"]))
                .order_by(JobModel.created_at.asc())
                .limit(SESSION_SUMMARY_PENDING_LIMIT),
            ),
        )

        lines = [
            "Session overview:",
            f"- Session title: {session_row.project_name}",
            f"- Target project: {session_row.target_project_name or session_row.project_name}",
            f"- Profile: {session_row.preset or 'unknown'}",
            f"- Status: {session_row.status}",
            f"- Current agent: {current_agent}",
            f"- Workdir: {session_row.workdir}",
        ]
        if session_row.launcher_id:
            lines.append(f"- Launcher: {session_row.launcher_id}")

        if inbound_entries:
            lines.append("")
            lines.append("Key user directions:")
            lines.extend(
                f"- {entry.actor}: {self._trim_context_text(entry.content, SESSION_SUMMARY_TEXT_LIMIT)}"
                for entry in inbound_entries
            )

        if outbound_entries:
            lines.append("")
            lines.append("Recent agent updates:")
            lines.extend(
                f"- {entry.actor}: {self._trim_context_text(entry.content, SESSION_SUMMARY_TEXT_LIMIT)}"
                for entry in outbound_entries
            )

        if system_entries:
            lines.append("")
            lines.append("System notes:")
            lines.extend(
                f"- {entry.actor}: {self._trim_context_text(entry.content, SESSION_SUMMARY_TEXT_LIMIT)}"
                for entry in system_entries
            )

        if pending_jobs:
            lines.append("")
            lines.append("Outstanding work:")
            lines.extend(
                f"- {job.agent_name} [{job.job_type}] {job.status}: "
                f"{self._trim_context_text(job.input_text, SESSION_SUMMARY_TEXT_LIMIT)}"
                for job in pending_jobs
            )

        return "\n".join(lines)

    def _load_transcript_direction(
        self,
        db: Session,
        *,
        session_id: str,
        direction: str,
        limit: int,
    ) -> list[TranscriptModel]:
        entries = list(
            db.scalars(
                select(TranscriptModel)
                .where(TranscriptModel.session_id == session_id)
                .where(TranscriptModel.direction == direction)
                .order_by(desc(TranscriptModel.created_at))
                .limit(limit),
            ),
        )
        entries.reverse()
        return entries

    def _extract_handoffs(
        self,
        output_text: str,
        *,
        agents: Iterable[AgentModel],
        source_agent: str,
    ) -> tuple[str, list[HandoffRequest], list[str]]:
        available_agents = {agent.agent_name.lower(): agent.agent_name for agent in agents}
        handoffs: list[HandoffRequest] = []
        rejections: list[str] = []

        def replace(match: re.Match[str]) -> str:
            if len(handoffs) >= MAX_HANDOFFS_PER_COMPLETION:
                LOGGER.warning(
                    "Ignoring extra handoff emitted by %s after reaching limit %s",
                    source_agent,
                    MAX_HANDOFFS_PER_COMPLETION,
                )
                rejections.append("extra handoff ignored after reaching the per-completion limit")
                return ""

            requested_name = match.group("agent").strip()
            target_agent = available_agents.get(requested_name.lower())
            body = match.group("body").strip()

            if target_agent is None:
                LOGGER.warning("Ignoring handoff from %s to unknown agent %s", source_agent, requested_name)
                rejections.append(f"handoff to unknown agent `{requested_name}`")
                return ""
            if target_agent.lower() == source_agent.lower():
                LOGGER.warning("Ignoring self-handoff emitted by %s", source_agent)
                rejections.append("self-handoff is not allowed")
                return ""
            if not body:
                LOGGER.warning("Ignoring empty handoff emitted by %s to %s", source_agent, target_agent)
                rejections.append(f"empty handoff for `{target_agent}`")
                return ""

            task_id = self._extract_task_id(body)
            validation_error = self._validate_handoff_body(body)
            if validation_error is not None:
                LOGGER.warning(
                    "Rejecting handoff from %s to %s: %s",
                    source_agent,
                    target_agent,
                    validation_error,
                )
                rejections.append(f"handoff for `{target_agent}` rejected: {validation_error}")
                return ""

            handoffs.append(HandoffRequest(target_agent=target_agent, body=body, task_id=task_id))
            return ""

        visible_output = HANDOFF_RE.sub(replace, output_text).strip()
        return visible_output, handoffs, rejections

    def _queue_handoffs(
        self,
        db: Session,
        *,
        session_id: str,
        source_agent: str,
        source_job: JobModel,
        handoffs: list[HandoffRequest],
    ) -> None:
        for handoff in handoffs:
            db.add(
                JobModel(
                    session_id=session_id,
                    agent_name=handoff.target_agent,
                    job_type="handoff",
                    source_discord_message_id=source_job.source_discord_message_id,
                    user_id=f"agent:{source_agent}",
                    input_text=handoff.body,
                    status="pending",
                ),
            )
            self.transcript_service.add_entry(
                db,
                session_id=session_id,
                direction="system",
                actor=source_agent,
                content=(
                    f"Handoff queued to {handoff.target_agent}"
                    f"{f' ({handoff.task_id})' if handoff.task_id else ''}: "
                    f"{self._trim_context_text(handoff.body, HANDOFF_PREVIEW_LIMIT)}"
                ),
                source_discord_message_id=source_job.source_discord_message_id,
            )

    def _record_handoff_rejections(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        source_agent: str,
        source_job: JobModel,
        rejections: list[str],
    ) -> None:
        if not rejections:
            return
        for rejection in rejections:
            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor="bridge",
                content=f"Handoff rejected from {source_agent}: {rejection}",
                source_discord_message_id=source_job.source_discord_message_id,
            )
        self._queue_planner_handoff_repair(
            db,
            session_row=session_row,
            source_agent=source_agent,
            source_job=source_job,
            rejections=rejections,
        )

    def _queue_planner_recovery(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        failed_job: JobModel,
        failed_agent: str,
        sanitized_error: str,
    ) -> bool:
        if failed_agent.lower() == "planner":
            return False
        if failed_job.job_type != "handoff":
            return False

        planner_agent = self._find_planner_agent(session_row.agents)
        if planner_agent is None:
            return False

        existing_recovery = db.scalar(
            select(JobModel)
            .where(JobModel.session_id == session_row.id)
            .where(JobModel.agent_name == planner_agent.agent_name)
            .where(JobModel.job_type == "recovery")
            .where(JobModel.status.in_(["pending", "in_progress"])),
        )
        if existing_recovery is not None:
            return False

        prompt = self._build_recovery_prompt(
            failed_agent=failed_agent,
            failed_job=failed_job,
            sanitized_error=sanitized_error,
        )
        db.add(
            JobModel(
                session_id=session_row.id,
                agent_name=planner_agent.agent_name,
                job_type="recovery",
                source_discord_message_id=failed_job.source_discord_message_id,
                user_id="system:recovery",
                input_text=prompt,
                status="pending",
            ),
        )
        self.transcript_service.add_entry(
            db,
            session_id=session_row.id,
            direction="system",
            actor="bridge",
            content=(
                f"Planner recovery queued after {failed_agent} {failed_job.job_type} failure: "
                f"{self._trim_context_text(sanitized_error, FAILURE_PREVIEW_LIMIT)}"
            ),
            source_discord_message_id=failed_job.source_discord_message_id,
        )
        return True

    def _queue_planner_handoff_repair(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        source_agent: str,
        source_job: JobModel,
        rejections: list[str],
    ) -> bool:
        planner_agent = self._find_planner_agent(session_row.agents)
        if planner_agent is None or source_agent.lower() == planner_agent.agent_name.lower():
            return False

        existing_job = db.scalar(
            select(JobModel)
            .where(JobModel.session_id == session_row.id)
            .where(JobModel.agent_name == planner_agent.agent_name)
            .where(JobModel.job_type.in_(["orchestration", "routing", "recovery", "handoff_repair"]))
            .where(JobModel.status.in_(["pending", "in_progress"])),
        )
        if existing_job is not None:
            return False

        db.add(
            JobModel(
                session_id=session_row.id,
                agent_name=planner_agent.agent_name,
                job_type="handoff_repair",
                source_discord_message_id=source_job.source_discord_message_id,
                user_id="system:handoff-validator",
                input_text=self._build_handoff_repair_prompt(
                    source_agent=source_agent,
                    source_job=source_job,
                    rejections=rejections,
                ),
                status="pending",
            ),
        )
        self.transcript_service.add_entry(
            db,
            session_id=session_row.id,
            direction="system",
            actor="bridge",
            content=(
                f"Planner handoff repair queued after {source_agent} emitted invalid handoff data: "
                f"{self._trim_context_text(' ; '.join(rejections), FAILURE_PREVIEW_LIMIT)}"
            ),
            source_discord_message_id=source_job.source_discord_message_id,
        )
        return True

    def _recover_orphaned_job(
        self,
        db: Session,
        *,
        session_id: str,
        agent: AgentModel,
        active_job: JobModel,
        worker_id: str,
    ) -> bool:
        if active_job.worker_id in (None, worker_id):
            return False
        if agent.worker_id != worker_id:
            return False

        LOGGER.warning(
            "Recovering orphaned in-progress job %s for session=%s agent=%s old_worker=%s new_worker=%s",
            active_job.id,
            session_id,
            agent.agent_name,
            active_job.worker_id,
            worker_id,
        )
        preview = self._trim_context_text(active_job.input_text, ORPHANED_JOB_PREVIEW_LIMIT)
        previous_worker_id = active_job.worker_id
        active_job.status = "pending"
        active_job.worker_id = None
        active_job.claimed_at = None
        active_job.completed_at = None
        self.transcript_service.add_entry(
            db,
            session_id=session_id,
            direction="system",
            actor="bridge",
            content=(
                f"Recovered orphaned job for {agent.agent_name} after worker replacement "
                f"({previous_worker_id} -> {worker_id}): {preview}"
            ),
            source_discord_message_id=active_job.source_discord_message_id,
        )
        return True

    def _record_outbound_thread_messages(
        self,
        *,
        session_id: str,
        agent_name: str,
        sent_chunks: list[tuple[str, str]],
    ) -> None:
        if not sent_chunks:
            return
        with session_scope() as db:
            for message_id, chunk in sent_chunks:
                self.transcript_service.add_entry(
                    db,
                    session_id=session_id,
                    direction="outbound",
                    actor=agent_name,
                    content=chunk,
                    source_discord_message_id=message_id,
                )

    def _format_agent_thread_message(
        self,
        *,
        agent_name: str,
        visible_output: str,
        handoffs: list[HandoffRequest],
        quiet_discord: bool,
    ) -> str:
        del handoffs
        body = self._quiet_discord_text(visible_output) if quiet_discord else visible_output.strip()
        if not body:
            return ""
        return f"**{agent_name}**\n{body}"

    def _format_failure_thread_message(
        self,
        *,
        agent_name: str,
        sanitized_error: str,
        recovery_queued: bool,
        quiet_discord: bool,
    ) -> str:
        failure_preview = self._trim_context_text(sanitized_error, FAILURE_PREVIEW_LIMIT)
        summary = self._quiet_discord_text(failure_preview) if quiet_discord else failure_preview
        if recovery_queued:
            return (
                f"**{agent_name} error**\n"
                f"{summary}\n\n"
                "Planner recovery has been queued to summarize the failure and decide the next step."
            )
        return f"**{agent_name} error**\n{summary}"

    def _quiet_discord_text(self, text: str) -> str:
        normalized_lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        if not normalized_lines:
            return ""
        trimmed_lines = [self._trim_context_text(line, 180) for line in normalized_lines[:QUIET_DISCORD_LINE_LIMIT]]
        if len(normalized_lines) > QUIET_DISCORD_LINE_LIMIT:
            trimmed_lines.append("...")
        compact = "\n".join(trimmed_lines)
        if len(compact) <= QUIET_DISCORD_CHAR_LIMIT:
            return compact
        return self._trim_context_text(compact, QUIET_DISCORD_CHAR_LIMIT)

    def _build_recovery_prompt(
        self,
        *,
        failed_agent: str,
        failed_job: JobModel,
        sanitized_error: str,
    ) -> str:
        failed_work = self._trim_context_text(failed_job.input_text, FAILURE_PREVIEW_LIMIT)
        failure_preview = self._trim_context_text(sanitized_error, FAILURE_PREVIEW_LIMIT)
        return (
            f"Recovery requested after `{failed_agent}` failed while handling `{failed_job.job_type}` work.\n\n"
            "Do not retry blindly. First stabilize the session state using the local workspace files.\n\n"
            "Required recovery steps:\n"
            "1. Read `CURRENT_STATE.md`, `STATUS.md`, `HANDOFFS.md`, and the latest relevant `AGENTS/*.md` / `RUN_LOGS/*.md` files.\n"
            "2. Write a short operator-facing `[[report]]...[[/report]]` that clearly says whether the session is blocked or can continue.\n"
            "3. If the work should continue, split it into a smaller handoff and queue only one focused next action.\n"
            "4. If you truly need the operator, ask exactly one critical `[[question]]...[[/question]]`.\n\n"
            f"Failed agent: `{failed_agent}`\n"
            f"Failed work summary: {failed_work}\n"
            f"Failure summary: {failure_preview}\n"
        )

    def _build_handoff_repair_prompt(
        self,
        *,
        source_agent: str,
        source_job: JobModel,
        rejections: list[str],
    ) -> str:
        rejected_preview = "\n".join(f"- {reason}" for reason in rejections)
        failed_work = self._trim_context_text(source_job.input_text, FAILURE_PREVIEW_LIMIT)
        return (
            f"Handoff repair requested after `{source_agent}` emitted invalid follow-up work.\n\n"
            "Rebuild the next steps using the task-card workflow.\n\n"
            "Required behavior:\n"
            "1. Read `CURRENT_STATE.md`, `TASK_BOARD.md`, `HANDOFFS.md`, and the relevant `AGENTS/*.md` / `RUN_LOGS/*.md` files.\n"
            "2. Refresh `TASK_BOARD.md` and any necessary `TASKS/T-###.md` cards before handing work off.\n"
            "3. Queue only disjoint, task-card-sized follow-up work.\n"
            "4. Every handoff body must contain a `T-###` line, a `Target summary:` line, and the exact reminder `Read CURRENT_STATE.md and TASK_BOARD.md first.`\n"
            "5. Keep each handoff compact and include `Files:` plus `Done condition:` so the next agent can execute it safely.\n"
            "6. Keep Discord output short: one `[[report]]...[[/report]]`, and only one critical `[[question]]...[[/question]]` if truly blocked.\n\n"
            f"Rejected handoff reasons:\n{rejected_preview}\n\n"
            f"Source agent: `{source_agent}`\n"
            f"Original work summary: {failed_work}\n"
        )

    def _build_planner_orchestration_prompt(self, user_text: str) -> str:
        request = user_text.strip() or "(empty message)"
        return (
            "Planner-first orchestration is required for the user request below.\n\n"
            f"Original request:\n{request}\n\n"
            "Required behavior:\n"
            "1. Update `TASK_BOARD.md` before implementation handoffs.\n"
            "2. Create or refresh any needed `TASKS/T-###.md` cards.\n"
            "3. Queue only disjoint task-card-sized handoffs that can run safely in parallel.\n"
            "4. Every handoff body must contain a `T-###` line, a `Target summary:` line, and the exact reminder `Read CURRENT_STATE.md and TASK_BOARD.md first.`\n"
            "5. Keep each handoff compact and include `Files:` plus `Done condition:` so the next agent can execute it safely.\n"
            "6. If work is not ready for implementation, keep ownership with planner and continue refining the local markdown artifacts.\n"
            "7. Keep Discord output to a short `[[report]]...[[/report]]`, and use `[[question]]...[[/question]]` only for truly critical blockers.\n"
        )

    def _build_planner_routing_prompt(
        self,
        *,
        user_text: str,
        last_active_agent: str | None,
        default_agent: str | None,
    ) -> str:
        request = user_text.strip() or "(empty message)"
        current_owner = last_active_agent or default_agent or "planner"
        return (
            "LLM-assisted routing is required for the plain-text operator message below.\n\n"
            f"Original request:\n{request}\n\n"
            "Routing goal:\n"
            "- Decide whether this is a follow-up to work already in progress, a new top-level request, or a truly blocking operator question.\n"
            f"- The most likely current owner is `{current_owner}` unless the local workspace says otherwise.\n\n"
            "Required behavior:\n"
            "1. Read `CURRENT_STATE.md`, `CURRENT_TASK.md`, `TASK_BOARD.md`, and the relevant `TASKS/*.md` / `AGENTS/*.md` notes first.\n"
            "2. If this is a continuation of work already underway, do not re-plan from scratch. Keep Discord output short and either continue with planner or hand off one focused next step.\n"
            "3. If this is a new top-level request, refresh `TASK_BOARD.md`, create or update any needed `TASKS/T-###.md` cards, and then queue only disjoint task-card-sized handoffs.\n"
            "4. Every handoff body must contain a `T-###` line, a `Target summary:` line, and the exact reminder `Read CURRENT_STATE.md and TASK_BOARD.md first.`\n"
            "5. Keep each handoff compact and include `Files:` plus `Done condition:` so the next agent can execute it safely.\n"
            "6. Keep Discord output to a short `[[report]]...[[/report]]`, and use `[[question]]...[[/question]]` only for truly critical blockers.\n"
        )

    @staticmethod
    def _find_planner_agent(agents: Iterable[AgentModel]) -> AgentModel | None:
        agent_list = list(agents)
        for agent in agent_list:
            if agent.agent_name.lower() == "planner":
                return agent
        for agent in agent_list:
            if "plan" in agent.role.lower():
                return agent
        return None

    def _should_route_to_planner_first(
        self,
        *,
        content: str,
        planner_agent: AgentModel | None,
    ) -> bool:
        if planner_agent is None:
            return False
        normalized = content.strip()
        if not normalized:
            return False
        if TASK_CARD_ID_RE.search(normalized):
            return False
        if FOLLOW_UP_MESSAGE_RE.match(normalized):
            return False
        if len(" ".join(normalized.split())) <= 48 and FOLLOW_UP_HINT_RE.search(normalized):
            return False
        return True

    def _validate_handoff_body(self, body: str) -> str | None:
        normalized = " ".join(body.split())
        if len(normalized) < MIN_HANDOFF_BODY_LENGTH:
            return "handoff is too short for a task-card-sized next step"
        if TASK_CARD_ID_RE.search(normalized) is None:
            return "handoff is missing a `T-###` task id"
        if "TASK_BOARD.md" not in body or "CURRENT_STATE.md" not in body:
            return "handoff must reference `TASK_BOARD.md` and `CURRENT_STATE.md`"
        if "Target summary:" not in body:
            return "handoff must include a compact target summary"
        return None

    @staticmethod
    def _extract_task_id(text: str) -> str | None:
        match = TASK_CARD_ID_RE.search(text or "")
        if match is None:
            return None
        return match.group(0).upper()

    @staticmethod
    def _trim_context_text(text: str, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _validate_session_start(
        self,
        manifest: ProjectManifest,
        *,
        user_id: str,
        guild_id: str,
        parent_channel_id: str,
    ) -> None:
        if manifest.guild_id != guild_id:
            raise PermissionError(
                "This project can only be started in its configured Discord guild.",
            )
        if manifest.parent_channel_id != parent_channel_id:
            raise PermissionError(
                "This project can only be started from its configured parent channel.",
            )
        if user_id not in manifest.allowed_user_ids:
            raise PermissionError(
                "You are not allowed to start sessions for this project.",
            )

    def _require_session(self, db: Session, statement) -> SessionModel:
        session_row = db.scalar(statement)
        if session_row is None:
            raise ValueError("Session not found.")
        return session_row

    def _require_agent(self, db: Session, *, session_id: str, agent_name: str) -> AgentModel:
        agent = db.scalar(
            select(AgentModel)
            .where(AgentModel.session_id == session_id)
            .where(AgentModel.agent_name == agent_name),
        )
        if agent is None:
            raise ValueError("Agent not found.")
        return agent

    def _require_project_find(self, db: Session, *, find_id: str) -> ProjectFindModel:
        row = db.scalar(
            select(ProjectFindModel).where(ProjectFindModel.id == find_id),
        )
        if row is None:
            raise ValueError("Project find request not found.")
        return row

    def _require_job(
        self,
        db: Session,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
    ) -> JobModel:
        job = db.scalar(
            select(JobModel)
            .where(JobModel.id == job_id)
            .where(JobModel.session_id == session_id)
            .where(JobModel.agent_name == agent_name),
        )
        if job is None:
            raise ValueError("Job not found.")
        if job.worker_id not in (None, worker_id):
            raise PermissionError("Job is owned by a different worker.")
        return job
