from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from uuid import uuid4

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, selectinload

from .behaviors.workflow.models import (
    HandoffModel,
    JobModel,
    ProjectFindModel,
    SessionOperationModel,
    SessionPolicyModel,
    TaskEventModel,
    TaskModel,
)
from .behaviors.workflow.schemas import (
    ArtifactHeartbeatSnapshot,
    HandoffStateSummary,
    JobPayload,
    PolicySetResponse,
    ProjectFindCandidate,
    ProjectFindCompleteRequest,
    ProjectFindLaunchResponse,
    ProjectFindSummaryResponse,
    ProjectManifest,
    SessionLaunchResponse,
    SessionOperationResponse,
    SessionPauseResponse,
    SessionPolicyResponse,
    SessionSummaryResponse,
    TaskStateSummary,
)
from .db import session_scope
from .kernel.drift import ArtifactSnapshot, DriftEvaluation, DriftMonitor
from .kernel.models import AgentModel, ExecutionTargetModel, PowerTargetModel, SessionModel, TranscriptModel
from .kernel.presence import (
    ActorSessionUpsertRequest,
    PresenceService,
    ResourceLeaseClaimRequest,
    ResourceLeaseHeartbeatRequest,
    ResourceLeaseReleaseRequest,
)
from .kernel.schemas import (
    AgentStatusResponse,
    ExecutionTargetSummary,
    PowerTargetSummary,
    ThreadDeltaEntry,
    ThreadDeltaResponse,
    TranscriptContextEntry,
)
from .thread_manager import ThreadManager
from .transcript_service import TranscriptService, sanitize_text
from .worker_registry import WorkerRegistry

LOGGER = logging.getLogger(__name__)
AGENT_PREFIX_RE = re.compile(r"^\s*@(?P<agent>[A-Za-z0-9_-]+)\s+(?P<body>.+)$", re.DOTALL)
AGENT_THREAD_HEADER_RE = re.compile(r"^\s*\*\*(?P<agent>[A-Za-z0-9_-]+)(?:\s+error)?\*\*")
TASK_CARD_ID_RE = re.compile(r"\bT-\d{3}\b", re.IGNORECASE)
CANONICAL_TASK_KEY_RE = re.compile(r"^T-(?P<number>\d+)$", re.IGNORECASE)
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
DISCUSS_RE = re.compile(
    r"\[\[discuss(?P<attrs>[^\]]*)\]\]\s*(?P<body>.*?)\s*\[\[/discuss\]\]",
    re.IGNORECASE | re.DOTALL,
)
RECENT_TRANSCRIPT_LIMIT = 12
RECENT_TRANSCRIPT_ENTRY_LIMIT = 800
HANDOFF_PREVIEW_LIMIT = 240
MAX_HANDOFFS_PER_COMPLETION = 4
MAX_DISCUSSIONS_PER_COMPLETION = 4
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
TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}
READY_TASK_STATES = {"ready", "review", "verify"}
TERMINAL_HANDOFF_STATES = {"consumed", "superseded", "failed"}
ATTACHMENT_SIGNAL_GRACE_SECONDS = 120
CURATION_READY_IDLE_SECONDS = 180
CURATION_PLANNER_IMBALANCE_SECONDS = 180
CURATION_ATTACHMENT_MISMATCH_SECONDS = 60
CURATION_SWEEP_COOLDOWN_SECONDS = 180
THREAD_DELTA_FETCH_LIMIT = 120
THREAD_CURSOR_SEPARATOR = "|"
OPS_TYPE_RE = re.compile(r"(^|\n)OPS:\s*type=(?P<type>[A-Za-z0-9_-]+)", re.IGNORECASE)
ORCHESTRATION_SCOPE_KIND = "orchestration_session"
ORCHESTRATION_JOB_RESOURCE_KIND = "orchestration_job"
ORCHESTRATION_PRESENCE_TTL_SECONDS = 180
ORCHESTRATION_LEASE_TTL_SECONDS = 180


@dataclass(slots=True)
class HandoffRequest:
    target_agent: str
    body: str
    task_id: str | None = None


@dataclass(slots=True)
class DiscussRequest:
    discuss_type: str
    body: str
    anomaly_id: str | None = None
    task_id: str | None = None
    ask_agents: list[str] | None = None
    to_agent: str | None = None


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
        presence_service: PresenceService | None = None,
    ) -> None:
        self.registry = registry
        self.thread_manager = thread_manager
        self.transcript_service = transcript_service
        self.drift_monitor = drift_monitor
        self.presence_service = presence_service or PresenceService()
        self.policy_service = None
        self.recovery_service = None
        self.start_workflow = None
        self.pause_workflow = None
        self.policy_workflow = None
        self.execution_provider = None
        self.announcement_service = None

    def bind_orchestration(
        self,
        *,
        policy_service,
        recovery_service,
        start_workflow,
        pause_workflow,
        policy_workflow,
        execution_provider,
        announcement_service,
    ) -> None:
        self.policy_service = policy_service
        self.recovery_service = recovery_service
        self.start_workflow = start_workflow
        self.pause_workflow = pause_workflow
        self.policy_workflow = policy_workflow
        self.execution_provider = execution_provider
        self.announcement_service = announcement_service

    def _upsert_orchestration_presence(
        self,
        *,
        db: Session,
        session_id: str,
        actor_id: str,
        status: str,
        ttl_seconds: int = ORCHESTRATION_PRESENCE_TTL_SECONDS,
    ) -> None:
        self.presence_service.upsert_actor_session(
            ActorSessionUpsertRequest(
                actor_id=actor_id,
                scope_kind=ORCHESTRATION_SCOPE_KIND,
                scope_id=session_id,
                status=status,
                ttl_seconds=ttl_seconds,
            ),
            db=db,
        )

    def _claim_orchestration_job_lease(
        self,
        *,
        db: Session,
        job: JobModel,
        actor_id: str,
        lease_seconds: int = ORCHESTRATION_LEASE_TTL_SECONDS,
    ) -> None:
        if not job.lease_token:
            return
        self.presence_service.claim_resource_lease(
            ResourceLeaseClaimRequest(
                resource_kind=ORCHESTRATION_JOB_RESOURCE_KIND,
                resource_id=job.id,
                holder_actor_id=actor_id,
                lease_token=job.lease_token,
                lease_seconds=lease_seconds,
                status=job.status,
            ),
            db=db,
        )

    def _heartbeat_orchestration_job_lease(
        self,
        *,
        db: Session,
        job: JobModel,
        actor_id: str,
        lease_seconds: int = ORCHESTRATION_LEASE_TTL_SECONDS,
    ) -> None:
        if not job.lease_token:
            return
        lease = self.presence_service.get_current_lease(
            resource_kind=ORCHESTRATION_JOB_RESOURCE_KIND,
            resource_id=job.id,
            db=db,
        )
        if lease is None:
            return
        self.presence_service.heartbeat_resource_lease(
            lease_id=lease.lease_id,
            payload=ResourceLeaseHeartbeatRequest(
                holder_actor_id=actor_id,
                lease_token=job.lease_token,
                lease_seconds=lease_seconds,
                status=job.status,
            ),
            db=db,
        )

    def _release_orchestration_job_lease(
        self,
        *,
        db: Session,
        job: JobModel,
        actor_id: str,
        status: str = "released",
    ) -> None:
        if not job.lease_token:
            return
        lease = self.presence_service.get_current_lease(
            resource_kind=ORCHESTRATION_JOB_RESOURCE_KIND,
            resource_id=job.id,
            db=db,
        )
        if lease is None:
            return
        self.presence_service.release_resource_lease(
            lease_id=lease.lease_id,
            payload=ResourceLeaseReleaseRequest(
                holder_actor_id=actor_id,
                lease_token=job.lease_token,
                status=status,
            ),
            db=db,
        )

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

    async def render_session_status_text(self, session_id: str) -> str:
        if self.announcement_service is not None:
            return await self.announcement_service.render_session_status_text(session_id)
        summary = await self.get_session_summary(session_id)
        return (
            f"Session `{summary.id}`\n"
            f"Session title: `{summary.project_name}`\n"
            f"Target project: `{summary.target_project_name or summary.project_name}`\n"
            f"Profile: `{summary.preset or 'unknown'}`\n"
            f"Workdir: `{summary.workdir}`\n"
            f"Status: `{summary.status}` (desired `{summary.desired_status}`)"
        )

    async def _sync_session_status_message(self, session_id: str, *, force: bool = False) -> None:
        if self.announcement_service is None:
            return
        await self.announcement_service.sync_session_status(session_id, force=force)

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
            self._close_session_row(
                db,
                session_row=session_row,
                closed_by=closed_by,
                transcript_content="Session closed from Discord.",
                job_error_text="Session closed.",
            )
            summary = self._to_summary_response(db, session_row)

        self.drift_monitor.clear_session(summary.id)
        await self.thread_manager.post_message(thread_id, "Session closed. New jobs will be rejected.")
        await self._sync_session_status_message(summary.id, force=True)
        await self.thread_manager.archive_thread(thread_id, "Session closed")
        return summary

    async def cleanup_session_thread(
        self,
        thread_id: str,
        closed_by: str,
        *,
        reason: str = "Session cleaned up from Discord.",
    ) -> SessionSummaryResponse:
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.discord_thread_id == thread_id),
            )
            if session_row.closed_at is None:
                self._close_session_row(
                    db,
                    session_row=session_row,
                    closed_by=closed_by,
                    transcript_content=reason,
                    job_error_text="Session cleaned up.",
                )
            summary = self._to_summary_response(db, session_row)

        self.drift_monitor.clear_session(summary.id)
        await self._sync_session_status_message(summary.id, force=True)
        await self.thread_manager.cleanup_thread(thread_id, "Session cleanup requested")
        return summary

    async def enqueue_restart(self, session_id: str, agent_name: str, requested_by: str) -> JobPayload:
        payload: JobPayload | None = None
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
                session_epoch=session_row.session_epoch,
                idempotency_key=f"restart:{session_row.id}:{agent_name}:{requested_by}",
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
            payload = self._to_job_payload(
                job,
                session_row=session_with_agents,
                session_summary=session_summary,
                recent_transcript=recent_transcript,
            )
        await self._sync_session_status_message(session_id)
        assert payload is not None
        return payload

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
            session_row.session_epoch += 1

            for job in db.scalars(
                select(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status == "pending"),
            ):
                job.status = "cancelled"
                job.completed_at = utcnow()
                job.error_text = "Cancelled by session reset."
            for job in db.scalars(
                select(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status == "in_progress"),
            ):
                self._release_orchestration_job_lease(
                    db=db,
                    job=job,
                    actor_id=job.agent_name,
                    status="released",
                )
                job.status = "cancelled"
                job.completed_at = utcnow()
                job.error_text = "Cancelled by session reset."

            for task in db.scalars(select(TaskModel).where(TaskModel.session_id == session_row.id)):
                task.current_lease_token = None
                task.current_worker_id = None
                task.session_epoch = session_row.session_epoch
                if task.state in {"in_progress", "review", "verify", "ready"}:
                    task.state = "ready"
                task.updated_at = utcnow()
                task.last_transition_at = utcnow()

            for handoff in db.scalars(select(HandoffModel).where(HandoffModel.session_id == session_row.id)):
                if handoff.state == "claimed":
                    handoff.state = "queued"
                handoff.session_epoch = session_row.session_epoch
                handoff.updated_at = utcnow()

            for agent in session_row.agents:
                self._upsert_orchestration_presence(
                    db=db,
                    session_id=session_row.id,
                    actor_id=agent.agent_name,
                    status="idle",
                    ttl_seconds=60,
                )
                db.add(
                    JobModel(
                        session_id=session_row.id,
                        agent_name=agent.agent_name,
                        job_type="restart",
                        user_id=requested_by,
                        input_text=f"Reset requested by {requested_by}",
                        status="pending",
                        session_epoch=session_row.session_epoch,
                        idempotency_key=f"reset:{session_row.id}:{agent.agent_name}:{requested_by}:{session_row.session_epoch}",
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
        await self._sync_session_status_message(session_id, force=True)

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
                    session_epoch=session_row.session_epoch,
                    idempotency_key=f"discord:{session_row.id}:{discord_message_id}:{routing.agent_name}",
                ),
            )
            LOGGER.info(
                "Queued job for session=%s agent=%s type=%s",
                session_row.id,
                routing.agent_name,
                routing.job_type,
            )
            session_id = session_row.id
            routed_agent = routing.agent_name
        await self._sync_session_status_message(session_id)
        return routed_agent

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
            now = utcnow()
            agent.worker_id = worker_id
            agent.pid_hint = pid_hint
            agent.status = "paused" if session_row.desired_status == "paused" else "idle"
            agent.last_heartbeat_at = now
            agent.last_error = None
            agent.current_activity_line = None
            agent.current_activity_updated_at = None
            session_row.execution_state = "online"
            session_row.status = "paused" if session_row.desired_status == "paused" else "launching"
            thread_id = session_row.discord_thread_id

            should_send_ready, ready_message = self._refresh_session_startup_state(
                db,
                session_row=session_row,
                now=now,
                previous_status=previous_status,
            )

            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor=agent_name,
                content=f"Worker {worker_id} registered for agent {agent_name}.",
            )
            self._upsert_orchestration_presence(
                db=db,
                session_id=session_id,
                actor_id=agent.agent_name,
                status=agent.status,
            )

        self.drift_monitor.register_worker(
            session_id=session_id,
            agent_name=agent_name,
            worker_id=worker_id,
            worker_status="idle",
        )
        if should_send_ready:
            await self.thread_manager.post_message(thread_id, ready_message)
        await self._sync_session_status_message(session_id)

    async def heartbeat(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
        status: str,
        pid_hint: int | None,
        artifact_snapshot: ArtifactHeartbeatSnapshot | None = None,
        activity_line: str | None = None,
    ) -> None:
        should_sync_status = False
        with session_scope() as db:
            session_row = self._require_session(
                db,
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            previous_session_status = session_row.status
            agent = self._require_agent(db, session_id=session_id, agent_name=agent_name)
            previous_status = agent.status
            agent.worker_id = worker_id
            agent.status = "paused" if agent.desired_status == "paused" and status != "busy" else status
            agent.pid_hint = pid_hint
            now = utcnow()
            agent.last_heartbeat_at = now
            normalized_activity = sanitize_text(activity_line or "").strip() or None
            if agent.status == "busy" and normalized_activity:
                agent.current_activity_line = normalized_activity
                agent.current_activity_updated_at = now
            else:
                agent.current_activity_line = None
                agent.current_activity_updated_at = None
            self._upsert_orchestration_presence(
                db=db,
                session_id=session_id,
                actor_id=agent.agent_name,
                status=agent.status,
            )
            active_job = db.scalar(
                select(JobModel)
                .where(JobModel.session_id == session_id)
                .where(JobModel.agent_name == agent_name)
                .where(JobModel.status == "in_progress"),
            )
            if active_job is not None:
                self._heartbeat_orchestration_job_lease(
                    db=db,
                    job=active_job,
                    actor_id=agent.agent_name,
                )

            self._refresh_session_startup_state(
                db,
                session_row=session_row,
                now=now,
                previous_status=previous_session_status,
            )

            any_busy = self._has_busy_agent(session_row.agents)
            active_job_count = int(
                db.scalar(
                    select(func.count())
                    .select_from(JobModel)
                    .where(JobModel.session_id == session_id)
                    .where(JobModel.status == "in_progress"),
                )
                or 0,
            )
            became_busy = previous_status != "busy" and agent.status == "busy"
            became_idle = previous_status == "busy" and agent.status != "busy"
            startup_status_changed = previous_session_status != session_row.status
            last_announced_at = session_row.last_announced_at
            if last_announced_at is not None and last_announced_at.tzinfo is None:
                last_announced_at = last_announced_at.replace(tzinfo=timezone.utc)
            report_due = (any_busy or active_job_count > 0) and (
                last_announced_at is None
                or (now - last_announced_at) >= timedelta(seconds=60)
            )
            should_sync_status = became_busy or became_idle or startup_status_changed or report_due
        self.drift_monitor.record_heartbeat(
            session_id=session_id,
            agent_name=agent_name,
            worker_id=worker_id,
            worker_status=status,
            artifact_snapshot=self._to_artifact_snapshot(artifact_snapshot),
        )
        if should_sync_status:
            await self._sync_session_status_message(session_id)

    def get_thread_delta(
        self,
        *,
        session_id: str,
        agent_name: str,
        cursor: str | None = None,
        kinds: list[str] | None = None,
        task_id: str | None = None,
        limit: int = 12,
    ) -> ThreadDeltaResponse:
        requested_kinds = {item.strip().lower() for item in (kinds or []) if item and item.strip()}
        normalized_task_id = task_id.strip().upper() if task_id else None
        cursor_marker = self._decode_thread_cursor(cursor)
        events: list[ThreadDeltaEntry] = []
        next_cursor = cursor
        with session_scope() as db:
            self._require_session(
                db,
                select(SessionModel).where(SessionModel.id == session_id),
            )
            self._require_agent(db, session_id=session_id, agent_name=agent_name)
            statement = select(TranscriptModel).where(TranscriptModel.session_id == session_id)
            if cursor_marker is None:
                transcript_rows = list(
                    db.scalars(
                        statement
                        .order_by(desc(TranscriptModel.created_at), desc(TranscriptModel.id))
                        .limit(THREAD_DELTA_FETCH_LIMIT),
                    ),
                )
                transcript_rows.reverse()
            else:
                transcript_rows = list(
                    db.scalars(
                        statement
                        .where(TranscriptModel.created_at >= cursor_marker[0])
                        .order_by(TranscriptModel.created_at.asc(), TranscriptModel.id.asc())
                        .limit(THREAD_DELTA_FETCH_LIMIT),
                    ),
                )

        scanned_after_cursor = False
        for entry in transcript_rows:
            if not self._thread_entry_after_cursor(entry, cursor_marker):
                continue
            scanned_after_cursor = True
            next_cursor = self._encode_thread_cursor(entry)
            kind = self._classify_transcript_kind(entry)
            if requested_kinds and kind not in requested_kinds:
                continue
            resolved_task_id = self._extract_transcript_task_id(entry.content)
            if normalized_task_id and resolved_task_id != normalized_task_id:
                continue
            events.append(
                ThreadDeltaEntry(
                    cursor=self._encode_thread_cursor(entry),
                    direction=entry.direction,
                    actor=entry.actor,
                    kind=kind,
                    content=entry.content,
                    task_id=resolved_task_id,
                    created_at=entry.created_at,
                ),
            )
            if len(events) >= limit:
                break

        if not scanned_after_cursor:
            next_cursor = cursor
        return ThreadDeltaResponse(next_cursor=next_cursor, events=events)

    async def claim_next_job(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
    ) -> JobPayload | None:
        payload: JobPayload | None = None
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
            if active_job_count >= max_parallel_agents and not self._is_curator_agent(agent):
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
                payload = self._claim_ready_task_job(
                    db,
                    session_row=session_row,
                    agent=agent,
                    worker_id=worker_id,
                )
                if payload is None:
                    if self._is_curator_agent(agent):
                        payload = self._claim_curator_sweep_job(
                            db,
                            session_row=session_row,
                            agent=agent,
                            worker_id=worker_id,
                        )
                    if payload is not None:
                        return payload
                    agent.status = "idle"
                    return None
                return payload

            job.status = "in_progress"
            job.claimed_at = utcnow()
            job.worker_id = worker_id
            if not job.lease_token:
                job.lease_token = str(uuid4())
            job.session_epoch = session_row.session_epoch
            agent.status = "busy"
            db.flush()
            db.refresh(job)
            self._claim_orchestration_job_lease(
                db=db,
                job=job,
                actor_id=agent.agent_name,
            )
            self._upsert_orchestration_presence(
                db=db,
                session_id=session_id,
                actor_id=agent.agent_name,
                status=agent.status,
            )
            recent_transcript = self._load_recent_transcript(db, session_id=session_id)
            session_summary = self._build_session_summary(
                db,
                session_row=session_row,
                current_agent=agent_name,
            )
            payload = self._to_job_payload(
                job,
                session_row=session_row,
                session_summary=session_summary,
                recent_transcript=recent_transcript,
            )
        if payload is not None:
            await self._sync_session_status_message(session_id)
        return payload

    async def complete_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        output_text: str,
        thread_output_text: str | None = None,
        lease_token: str | None = None,
        task_revision: int | None = None,
        session_epoch: int | None = None,
        pid_hint: int | None,
    ) -> None:
        sanitized = sanitize_text(output_text)
        sanitized_thread = sanitize_text(thread_output_text) if thread_output_text else ""
        thread_id = ""
        thread_message = ""
        visible_output = ""
        display_output = ""
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
            if job.status in TERMINAL_JOB_STATES:
                return
            if not self._validate_job_concurrency(
                job=job,
                session_row=session_row,
                lease_token=lease_token,
                task_revision=task_revision,
                session_epoch=session_epoch,
            ):
                return
            policy = self._load_policy_summary(db, session_row)
            quiet_discord = policy.quiet_discord if policy is not None else True
            visible_output, handoffs, rejected_handoffs, discussions, rejected_discussions = self._extract_control_updates(
                sanitized,
                agents=session_row.agents,
                source_agent=agent_name,
            )
            display_output = sanitized_thread or visible_output

            job.status = "completed"
            job.result_text = display_output
            job.completed_at = utcnow()
            self._release_orchestration_job_lease(
                db=db,
                job=job,
                actor_id=agent.agent_name,
                status="released",
            )

            agent.status = "paused" if session_row.desired_status == "paused" else "idle"
            agent.pid_hint = pid_hint
            agent.last_heartbeat_at = utcnow()
            agent.last_error = None
            agent.current_activity_line = None
            agent.current_activity_updated_at = None
            self._upsert_orchestration_presence(
                db=db,
                session_id=session_id,
                actor_id=agent.agent_name,
                status=agent.status,
            )
            thread_id = session_row.discord_thread_id

            self._queue_handoffs(
                db,
                session_row=session_row,
                session_id=session_id,
                source_agent=agent_name,
                source_job=job,
                handoffs=handoffs,
            )
            self._queue_discussions(
                db,
                session_id=session_id,
                source_agent=agent_name,
                source_job=job,
                discussions=discussions,
            )
            if rejected_handoffs:
                self._record_handoff_rejections(
                    db,
                    session_row=session_row,
                    source_agent=agent_name,
                    source_job=job,
                    rejections=rejected_handoffs,
                )
            if rejected_discussions:
                self._record_discussion_rejections(
                    db,
                    session_id=session_id,
                    source_agent=agent_name,
                    source_job=job,
                    rejections=rejected_discussions,
                )
            self._synchronize_completed_task(
                db,
                session_row=session_row,
                job=job,
                actor=agent_name,
                report_text=visible_output,
                question_present="[[question]]" in sanitized.lower(),
                handoffs=handoffs,
            )
            thread_message = self._format_agent_thread_message(
                agent_name=agent_name,
                visible_output=display_output,
                handoffs=handoffs,
                quiet_discord=quiet_discord,
            )

        if thread_message:
            sent_chunks = await self.thread_manager.post_message(thread_id, thread_message)
            if display_output:
                self._record_outbound_thread_messages(
                    session_id=session_id,
                    agent_name=agent_name,
                    sent_chunks=sent_chunks,
                )
        await self._sync_session_status_message(session_id)

    async def fail_job(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        worker_id: str,
        error_text: str,
        lease_token: str | None = None,
        task_revision: int | None = None,
        session_epoch: int | None = None,
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
            if job.status in TERMINAL_JOB_STATES:
                return
            if not self._validate_job_concurrency(
                job=job,
                session_row=session_row,
                lease_token=lease_token,
                task_revision=task_revision,
                session_epoch=session_epoch,
            ):
                return
            policy = self._load_policy_summary(db, session_row)
            quiet_discord = policy.quiet_discord if policy is not None else True

            job.status = "failed"
            job.error_text = sanitized
            job.completed_at = utcnow()
            self._release_orchestration_job_lease(
                db=db,
                job=job,
                actor_id=agent.agent_name,
                status="released",
            )

            agent.status = "paused" if session_row.desired_status == "paused" else "idle"
            agent.pid_hint = pid_hint
            agent.last_heartbeat_at = utcnow()
            agent.last_error = sanitized
            agent.current_activity_line = None
            agent.current_activity_updated_at = None
            self._upsert_orchestration_presence(
                db=db,
                session_id=session_id,
                actor_id=agent.agent_name,
                status=agent.status,
            )
            thread_id = session_row.discord_thread_id
            self._synchronize_failed_task(
                db,
                session_row=session_row,
                job=job,
                actor=agent_name,
                error_text=sanitized,
            )

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
        await self._sync_session_status_message(session_id)

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
        pending_jobs = int(
            db.scalar(
                select(func.count())
                .select_from(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status == "pending"),
            )
            or 0,
        )
        active_jobs = int(
            db.scalar(
                select(func.count())
                .select_from(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status == "in_progress"),
            )
            or 0,
        )
        tasks = list(
            db.scalars(
                select(TaskModel)
                .where(TaskModel.session_id == session_row.id)
                .order_by(TaskModel.created_at.asc()),
            ),
        )
        queued_handoffs = list(
            db.scalars(
                select(HandoffModel)
                .options(selectinload(HandoffModel.task))
                .where(HandoffModel.session_id == session_row.id)
                .where(HandoffModel.state == "queued")
                .order_by(HandoffModel.created_at.asc()),
            ),
        )
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
            session_epoch=session_row.session_epoch,
            created_at=session_row.created_at,
            closed_at=session_row.closed_at,
            power_target=self._load_power_target_summary(db, session_row),
            execution_target=self._load_execution_target_summary(db, session_row),
            policy=self._load_policy_summary(db, session_row),
            active_operation=self._load_active_operation(db, session_row),
            pending_jobs=pending_jobs,
            active_jobs=active_jobs,
            tasks=[self._to_task_summary(task) for task in tasks],
            queued_handoffs=[self._to_handoff_summary(handoff) for handoff in queued_handoffs],
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
        if not available_presets:
            raise ValueError(
                "No profiles are currently registered by any launcher. "
                "Start the PC launcher first, then run /project start again.",
            )
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
            current_activity_line=agent.current_activity_line,
            current_activity_updated_at=agent.current_activity_updated_at,
            drift_state=drift.drift_state,
            drift_reason=drift.drift_reason,
            workspace_ready=drift.workspace_ready,
            last_artifact_at=drift.last_artifact_at,
            last_artifact_path=drift.last_artifact_path,
            current_task_id=drift.current_task_id,
            current_task_state=drift.current_task_state,
        )

    @staticmethod
    def _to_task_summary(task: TaskModel) -> TaskStateSummary:
        file_scope: list[str] = []
        if task.file_scope_json:
            try:
                raw_scope = json.loads(task.file_scope_json)
                if isinstance(raw_scope, list):
                    file_scope = [str(item) for item in raw_scope if str(item).strip()]
            except Exception:  # noqa: BLE001
                file_scope = []
        return TaskStateSummary(
            id=task.id,
            task_key=task.task_key,
            title=task.title,
            role=task.role,
            assigned_agent=task.assigned_agent,
            source_agent=task.source_agent,
            depends_on_task_key=task.depends_on_task_key,
            semantic_scope=task.semantic_scope,
            file_scope=file_scope,
            state=task.state,
            revision=task.revision,
            session_epoch=task.session_epoch,
            summary_text=task.summary_text,
            body_text=task.body_text,
            latest_brief_name=task.latest_brief_name,
            latest_log_name=task.latest_log_name,
            created_at=task.created_at,
            updated_at=task.updated_at,
            last_transition_at=task.last_transition_at,
        )

    @staticmethod
    def _to_handoff_summary(handoff: HandoffModel) -> HandoffStateSummary:
        return HandoffStateSummary(
            id=handoff.id,
            task_id=handoff.task_id,
            task_key=handoff.task.task_key if handoff.task is not None else "",
            source_agent=handoff.source_agent,
            target_agent=handoff.target_agent,
            target_role=handoff.target_role,
            state=handoff.state,
            revision=handoff.revision,
            session_epoch=handoff.session_epoch,
            body_text=handoff.body_text,
            created_at=handoff.created_at,
            claimed_at=handoff.claimed_at,
            consumed_at=handoff.consumed_at,
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
            task_id=job.task_id,
            task_revision=job.task_revision,
            lease_token=job.lease_token,
            session_epoch=job.session_epoch,
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

    @staticmethod
    def _encode_thread_cursor(entry: TranscriptModel) -> str:
        return f"{entry.created_at.isoformat()}{THREAD_CURSOR_SEPARATOR}{entry.id}"

    @staticmethod
    def _decode_thread_cursor(cursor: str | None) -> tuple[datetime, str] | None:
        if not cursor:
            return None
        raw_created_at, separator, raw_id = cursor.partition(THREAD_CURSOR_SEPARATOR)
        if not separator or not raw_created_at or not raw_id:
            return None
        try:
            created_at = datetime.fromisoformat(raw_created_at)
        except ValueError:
            return None
        return created_at, raw_id

    @staticmethod
    def _thread_entry_after_cursor(
        entry: TranscriptModel,
        cursor_marker: tuple[datetime, str] | None,
    ) -> bool:
        if cursor_marker is None:
            return True
        cursor_created_at, cursor_id = cursor_marker
        if entry.created_at > cursor_created_at:
            return True
        if entry.created_at < cursor_created_at:
            return False
        return entry.id > cursor_id

    def _classify_transcript_kind(self, entry: TranscriptModel) -> str:
        content = entry.content.strip()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if entry.direction == "inbound":
            return "user"
        if entry.direction == "system":
            lowered = content.lower()
            if lowered.startswith("discussion queued"):
                return "discuss_open"
            if lowered.startswith("discussion reply queued"):
                return "discuss_reply"
            return "status"
        if any(line.startswith("ISSUE:") for line in lines):
            return "issue"
        if any(line.startswith("ANSWER:") for line in lines):
            return "answer"
        if any(line.startswith("DONE:") for line in lines):
            return "done"
        if any(line.startswith("HUMAN:") for line in lines):
            return "human"
        match = OPS_TYPE_RE.search(content)
        if match is not None:
            return match.group("type").strip().lower()
        if any(line.startswith("OPS:") for line in lines):
            return "ops"
        return "status"

    @staticmethod
    def _extract_transcript_task_id(content: str) -> str | None:
        match = TASK_CARD_ID_RE.search(content)
        if match is None:
            return None
        return match.group(0).strip().upper()

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
        ready_tasks = list(
            db.scalars(
                select(TaskModel)
                .where(TaskModel.session_id == session_row.id)
                .where(TaskModel.state.in_(tuple(sorted(READY_TASK_STATES))))
                .order_by(TaskModel.updated_at.asc())
                .limit(SESSION_SUMMARY_PENDING_LIMIT),
            ),
        )
        queued_handoffs = list(
            db.scalars(
                select(HandoffModel)
                .options(selectinload(HandoffModel.task))
                .where(HandoffModel.session_id == session_row.id)
                .where(HandoffModel.state == "queued")
                .order_by(HandoffModel.created_at.asc())
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
        attached_agents = self._attached_agent_count(session_row.agents)
        total_agents = len(session_row.agents)
        active_jobs = sum(1 for job in pending_jobs if job.status == "in_progress")
        lines.append(f"- Worker attachment: {attached_agents}/{total_agents} attached")
        if 0 < attached_agents < total_agents:
            lines.append(
                f"- Startup note: work may already be running, but worker attachment is still incomplete "
                f"({attached_agents}/{total_agents}, active jobs={active_jobs})"
            )
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

        if ready_tasks:
            lines.append("")
            lines.append("Ready queue:")
            lines.extend(
                f"- {task.task_key} [{task.role}] {task.state} owner={task.assigned_agent or 'unassigned'}: "
                f"{self._trim_context_text(task.summary_text or task.title, SESSION_SUMMARY_TEXT_LIMIT)}"
                for task in ready_tasks
            )

        if queued_handoffs:
            lines.append("")
            lines.append("Queued handoffs:")
            lines.extend(
                f"- {handoff.task.task_key if handoff.task is not None else 'task-pending'} "
                f"{handoff.source_agent}->{handoff.target_agent}: "
                f"{self._trim_context_text(handoff.body_text, SESSION_SUMMARY_TEXT_LIMIT)}"
                for handoff in queued_handoffs
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

    def _extract_control_updates(
        self,
        output_text: str,
        *,
        agents: Iterable[AgentModel],
        source_agent: str,
    ) -> tuple[str, list[HandoffRequest], list[str], list[DiscussRequest], list[str]]:
        available_agents = {agent.agent_name.lower(): agent.agent_name for agent in agents}
        handoffs: list[HandoffRequest] = []
        handoff_rejections: list[str] = []
        discussions: list[DiscussRequest] = []
        discussion_rejections: list[str] = []

        def replace_handoff(match: re.Match[str]) -> str:
            if len(handoffs) >= MAX_HANDOFFS_PER_COMPLETION:
                LOGGER.warning(
                    "Ignoring extra handoff emitted by %s after reaching limit %s",
                    source_agent,
                    MAX_HANDOFFS_PER_COMPLETION,
                )
                handoff_rejections.append("extra handoff ignored after reaching the per-completion limit")
                return ""

            requested_name = match.group("agent").strip()
            target_agent = available_agents.get(requested_name.lower())
            body = match.group("body").strip()

            if target_agent is None:
                LOGGER.warning("Ignoring handoff from %s to unknown agent %s", source_agent, requested_name)
                handoff_rejections.append(f"handoff to unknown agent `{requested_name}`")
                return ""
            if target_agent.lower() == source_agent.lower():
                LOGGER.warning("Ignoring self-handoff emitted by %s", source_agent)
                handoff_rejections.append("self-handoff is not allowed")
                return ""
            if not body:
                LOGGER.warning("Ignoring empty handoff emitted by %s to %s", source_agent, target_agent)
                handoff_rejections.append(f"empty handoff for `{target_agent}`")
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
                handoff_rejections.append(f"handoff for `{target_agent}` rejected: {validation_error}")
                return ""

            handoffs.append(HandoffRequest(target_agent=target_agent, body=body, task_id=task_id))
            return ""

        def replace_discuss(match: re.Match[str]) -> str:
            if len(discussions) >= MAX_DISCUSSIONS_PER_COMPLETION:
                LOGGER.warning(
                    "Ignoring extra discuss block emitted by %s after reaching limit %s",
                    source_agent,
                    MAX_DISCUSSIONS_PER_COMPLETION,
                )
                discussion_rejections.append("extra discuss block ignored after reaching the per-completion limit")
                return ""

            body = match.group("body").strip()
            attrs = self._parse_discuss_attrs(match.group("attrs") or "")
            discuss_type = (attrs.get("type") or "open").strip().lower()
            anomaly_id = (attrs.get("anomaly") or attrs.get("id") or "").strip() or None
            task_id = self._extract_task_id(body)

            if discuss_type not in {"open", "reply", "resolve", "escalate"}:
                discussion_rejections.append(f"unsupported discuss type `{discuss_type or 'none'}`")
                return ""
            if not body:
                discussion_rejections.append(f"empty discuss block for `{discuss_type}`")
                return ""

            ask_raw = (attrs.get("ask") or attrs.get("with") or "").strip()
            ask_names = [item.strip() for item in ask_raw.split(",") if item.strip()]
            resolved_ask = [available_agents[name.lower()] for name in ask_names if name.lower() in available_agents]
            invalid_ask = [name for name in ask_names if name.lower() not in available_agents]
            if invalid_ask:
                discussion_rejections.append(
                    f"discussion references unknown agent(s): {', '.join(f'`{name}`' for name in invalid_ask)}"
                )
                return ""

            to_name = (attrs.get("to") or "").strip()
            target_agent = available_agents.get(to_name.lower()) if to_name else None
            if to_name and target_agent is None:
                discussion_rejections.append(f"discussion target unknown agent `{to_name}`")
                return ""

            if discuss_type == "open":
                resolved_ask = [name for name in resolved_ask if name.lower() != source_agent.lower()]
                if not resolved_ask:
                    discussion_rejections.append("discussion_open requires at least one other agent in `ask=`")
                    return ""
            if discuss_type == "reply" and not target_agent:
                discussion_rejections.append("discussion_reply requires a valid `to=` target")
                return ""

            discussions.append(
                DiscussRequest(
                    discuss_type=discuss_type,
                    body=body,
                    anomaly_id=anomaly_id,
                    task_id=task_id,
                    ask_agents=resolved_ask or None,
                    to_agent=target_agent,
                ),
            )
            return ""

        visible_output = HANDOFF_RE.sub(replace_handoff, output_text)
        visible_output = DISCUSS_RE.sub(replace_discuss, visible_output).strip()
        return visible_output, handoffs, handoff_rejections, discussions, discussion_rejections

    def _queue_handoffs(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        session_id: str,
        source_agent: str,
        source_job: JobModel,
        handoffs: list[HandoffRequest],
    ) -> None:
        agent_roles = {agent.agent_name: agent.role for agent in session_row.agents}
        curator_details: list[str] = []
        for handoff in handoffs:
            task, normalized_body, duplicate = self._upsert_task_for_handoff(
                db,
                session_row=session_row,
                source_agent=source_agent,
                handoff=handoff,
            )
            if duplicate:
                self.transcript_service.add_entry(
                    db,
                    session_id=session_id,
                    direction="system",
                    actor="bridge",
                    content=(
                        f"Duplicate handoff ignored for {handoff.target_agent}"
                        f"{f' ({task.task_key})' if task is not None else ''}: "
                        f"{self._trim_context_text(normalized_body, HANDOFF_PREVIEW_LIMIT)}"
                    ),
                    source_discord_message_id=source_job.source_discord_message_id,
                )
                continue
            for stale_handoff in db.scalars(
                select(HandoffModel)
                .where(HandoffModel.task_id == task.id)
                .where(HandoffModel.state.in_(["queued", "claimed"])),
            ):
                stale_handoff.state = "superseded"
                stale_handoff.updated_at = utcnow()
                stale_handoff.consumed_at = stale_handoff.consumed_at or utcnow()
            handoff_row = HandoffModel(
                session_id=session_id,
                task_id=task.id,
                source_job_id=source_job.id,
                source_agent=source_agent,
                target_agent=handoff.target_agent,
                target_role=agent_roles.get(handoff.target_agent, task.role),
                state="queued",
                revision=task.revision,
                session_epoch=session_row.session_epoch,
                body_text=normalized_body,
            )
            db.add(handoff_row)
            db.flush()
            self._append_task_event(
                db,
                session_id=session_id,
                task=task,
                handoff=handoff_row,
                event_type="handoff_queued",
                actor=source_agent,
                payload={
                    "target_agent": handoff.target_agent,
                    "task_key": task.task_key,
                    "revision": task.revision,
                },
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
            curator_details.append(
                f"{task.task_key} -> {handoff.target_agent}: {self._trim_context_text(task.summary_text or normalized_body, HANDOFF_PREVIEW_LIMIT)}",
            )
        if curator_details:
            self._queue_curator_event(
                db,
                session_row=session_row,
                source_agent=source_agent,
                source_job=source_job,
                reason="handoff_update",
                detail_lines=curator_details,
            )

    @staticmethod
    def _append_task_event(
        db: Session,
        *,
        session_id: str,
        task: TaskModel | None,
        handoff: HandoffModel | None,
        event_type: str,
        actor: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        db.add(
            TaskEventModel(
                session_id=session_id,
                task_id=task.id if task is not None else None,
                handoff_id=handoff.id if handoff is not None else None,
                event_type=event_type,
                actor=actor,
                payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
            ),
        )

    def _upsert_task_for_handoff(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        source_agent: str,
        handoff: HandoffRequest,
    ) -> tuple[TaskModel, str, bool]:
        target_agent = self._require_agent(db, session_id=session_row.id, agent_name=handoff.target_agent)
        normalized_body = self._normalize_task_payload_text(handoff.body)
        task_key = (
            handoff.task_id.strip().upper()
            if handoff.task_id and CANONICAL_TASK_KEY_RE.fullmatch(handoff.task_id.strip().upper())
            else self._allocate_task_key(db, session_id=session_row.id)
        )
        summary_line = self._extract_prefixed_line(normalized_body, "Target summary:")
        file_scope = self._extract_prefixed_line(normalized_body, "Files:")
        done_condition = self._extract_prefixed_line(normalized_body, "Done condition:")
        semantic_scope = self._derive_semantic_scope(summary_line or normalized_body)

        task = db.scalar(
            select(TaskModel)
            .where(TaskModel.session_id == session_row.id)
            .where(TaskModel.task_key == task_key),
        )
        duplicate = False
        if task is None:
            task = TaskModel(
                session_id=session_row.id,
                task_key=task_key,
                title=self._derive_task_title_from_handoff(normalized_body),
                role=target_agent.role,
                assigned_agent=handoff.target_agent,
                source_agent=source_agent,
                state="ready",
                revision=1,
                session_epoch=session_row.session_epoch,
                summary_text=summary_line,
                body_text=normalized_body,
                semantic_scope=semantic_scope,
                file_scope_json=json.dumps(
                    [item.strip() for item in (file_scope or "").split(",") if item.strip()],
                    ensure_ascii=False,
                )
                if file_scope
                else None,
            )
            db.add(task)
            db.flush()
        else:
            duplicate = self._is_duplicate_ready_handoff(
                db,
                session_row=session_row,
                task=task,
                target_agent=handoff.target_agent,
                normalized_body=normalized_body,
            )
            if duplicate:
                return task, normalized_body, True
            task.title = self._derive_task_title_from_handoff(normalized_body)
            task.role = target_agent.role
            task.assigned_agent = handoff.target_agent
            task.source_agent = source_agent
            task.summary_text = summary_line
            task.body_text = normalized_body
            task.semantic_scope = semantic_scope
            task.file_scope_json = (
                json.dumps(
                    [item.strip() for item in (file_scope or "").split(",") if item.strip()],
                    ensure_ascii=False,
                )
                if file_scope
                else None
            )
            task.state = "ready"
            task.revision += 1
            task.session_epoch = session_row.session_epoch
            task.current_lease_token = None
            task.current_worker_id = None
            task.updated_at = utcnow()
            task.last_transition_at = utcnow()

        if done_condition:
            task.latest_brief_name = None
            task.latest_log_name = None
        return task, normalized_body, False

    @staticmethod
    def _is_duplicate_ready_handoff(
        db: Session,
        *,
        session_row: SessionModel,
        task: TaskModel,
        target_agent: str,
        normalized_body: str,
    ) -> bool:
        if task.session_epoch != session_row.session_epoch:
            return False
        if task.assigned_agent and task.assigned_agent.lower() != target_agent.lower():
            return False
        if (task.body_text or "").strip() != normalized_body.strip():
            return False
        if task.state not in {"ready", "in_progress", "review", "verify", "handoff_queued"}:
            return False
        existing_handoff = db.scalar(
            select(HandoffModel)
            .where(HandoffModel.session_id == session_row.id)
            .where(HandoffModel.task_id == task.id)
            .where(HandoffModel.target_agent == target_agent)
            .where(HandoffModel.state.in_(["queued", "claimed"]))
            .order_by(HandoffModel.created_at.desc()),
        )
        return existing_handoff is not None

    def _allocate_task_key(self, db: Session, *, session_id: str) -> str:
        existing_keys = db.scalars(
            select(TaskModel.task_key).where(TaskModel.session_id == session_id),
        ).all()
        values = [
            int(match.group("number"))
            for task_key in existing_keys
            if task_key and (match := CANONICAL_TASK_KEY_RE.fullmatch(task_key.strip().upper())) is not None
        ]
        next_index = (max(values) + 1) if values else 1
        return f"T-{next_index:03d}"

    def _claim_ready_task_job(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        agent: AgentModel,
        worker_id: str,
    ) -> JobPayload | None:
        role_candidates = {agent.role, agent.agent_name}
        handoff = next(
            (
                candidate
                for candidate in db.scalars(
                    select(HandoffModel)
                    .options(selectinload(HandoffModel.task))
                    .where(HandoffModel.session_id == session_row.id)
                    .where(HandoffModel.state == "queued")
                    .where(HandoffModel.session_epoch == session_row.session_epoch)
                    .where(
                        (HandoffModel.target_agent == agent.agent_name)
                        | (HandoffModel.target_role.in_(tuple(role_candidates)))
                    )
                    .order_by(HandoffModel.created_at.asc()),
                )
                if candidate.task is not None and self._can_self_claim_task(db, session_row=session_row, task=candidate.task)
            ),
            None,
        )
        if handoff is None or handoff.task is None:
            return None

        task = handoff.task
        if task.state not in READY_TASK_STATES or task.session_epoch != session_row.session_epoch:
            return None

        lease_token = str(uuid4())
        job = JobModel(
            session_id=session_row.id,
            agent_name=agent.agent_name,
            job_type=self._job_type_for_task(task),
            source_discord_message_id=None,
            user_id=f"agent:{handoff.source_agent}",
            input_text=handoff.body_text,
            status="in_progress",
            worker_id=worker_id,
            task_id=task.id,
            handoff_id=handoff.id,
            session_epoch=session_row.session_epoch,
            task_revision=task.revision,
            lease_token=lease_token,
            idempotency_key=f"{session_row.id}:{task.task_key}:{task.revision}:{agent.agent_name}",
            claimed_at=utcnow(),
        )
        db.add(job)
        db.flush()
        db.refresh(job)

        task.state = "in_progress"
        task.current_lease_token = lease_token
        task.current_worker_id = worker_id
        task.assigned_agent = agent.agent_name
        task.updated_at = utcnow()
        task.last_transition_at = utcnow()

        handoff.state = "claimed"
        handoff.claimed_by_job_id = job.id
        handoff.claimed_at = utcnow()
        handoff.updated_at = utcnow()

        self._append_task_event(
            db,
            session_id=session_row.id,
            task=task,
            handoff=handoff,
            event_type="task_claimed",
            actor=agent.agent_name,
            payload={"job_id": job.id, "lease_token": lease_token, "revision": task.revision},
        )
        self._claim_orchestration_job_lease(
            db=db,
            job=job,
            actor_id=agent.agent_name,
        )
        self._upsert_orchestration_presence(
            db=db,
            session_id=session_row.id,
            actor_id=agent.agent_name,
            status="busy",
        )

        recent_transcript = self._load_recent_transcript(db, session_id=session_row.id)
        session_summary = self._build_session_summary(
            db,
            session_row=session_row,
            current_agent=agent.agent_name,
        )
        agent.status = "busy"
        return self._to_job_payload(
            job,
            session_row=session_row,
            session_summary=session_summary,
            recent_transcript=recent_transcript,
        )

    def _claim_curator_sweep_job(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        agent: AgentModel,
        worker_id: str,
    ) -> JobPayload | None:
        if not self._is_curator_agent(agent):
            return None
        prompt, reason = self._build_curator_sweep_prompt(db, session_row=session_row)
        if not prompt:
            return None
        if not self._has_non_curator_busy_agent(session_row.agents) and reason not in {"partial_attachment"}:
            return None

        lease_token = str(uuid4())
        job = JobModel(
            session_id=session_row.id,
            agent_name=agent.agent_name,
            job_type="curation_sweep",
            source_discord_message_id=None,
            user_id="system:curator",
            input_text=prompt,
            status="in_progress",
            worker_id=worker_id,
            session_epoch=session_row.session_epoch,
            lease_token=lease_token,
            idempotency_key=f"curation_sweep:{session_row.id}:{session_row.session_epoch}:{reason}",
            claimed_at=utcnow(),
        )
        db.add(job)
        agent.status = "busy"
        self.transcript_service.add_entry(
            db,
            session_id=session_row.id,
            direction="system",
            actor="bridge",
            content=f"Curator sweep started: {reason}",
        )
        db.flush()
        db.refresh(job)
        self._claim_orchestration_job_lease(
            db=db,
            job=job,
            actor_id=agent.agent_name,
        )
        self._upsert_orchestration_presence(
            db=db,
            session_id=session_row.id,
            actor_id=agent.agent_name,
            status="busy",
        )
        recent_transcript = self._load_recent_transcript(db, session_id=session_row.id)
        session_summary = self._build_session_summary(
            db,
            session_row=session_row,
            current_agent=agent.agent_name,
        )
        return self._to_job_payload(
            job,
            session_row=session_row,
            session_summary=session_summary,
            recent_transcript=recent_transcript,
        )

    def _build_curator_sweep_prompt(
        self,
        db: Session,
        *,
        session_row: SessionModel,
    ) -> tuple[str, str] | None:
        now = utcnow()
        ready_threshold = now - timedelta(seconds=CURATION_READY_IDLE_SECONDS)
        stale_handoff = db.scalar(
            select(HandoffModel)
            .options(selectinload(HandoffModel.task))
            .where(HandoffModel.session_id == session_row.id)
            .where(HandoffModel.state == "queued")
            .where(HandoffModel.created_at <= ready_threshold)
            .order_by(HandoffModel.created_at.asc()),
        )
        if stale_handoff is not None and stale_handoff.task is not None:
            summary = self._trim_context_text(stale_handoff.body_text, HANDOFF_PREVIEW_LIMIT)
            return (
                (
                    "Curator sweep triggered because a queued handoff has been idle for too long.\n\n"
                    "Read CURRENT_STATE.md, TASK_BOARD.md, HANDOFFS.md, and the referenced task card first.\n"
                    "Do not create new scope. Re-direct, requeue, open discuss, or escalate only if necessary.\n\n"
                    f"Reason: stale queued handoff\n"
                    f"Task: {stale_handoff.task.task_key}\n"
                    f"Target agent: {stale_handoff.target_agent}\n"
                    f"Context: {summary}\n"
                ),
                "stale_handoff",
            )

        stale_task = db.scalar(
            select(TaskModel)
            .where(TaskModel.session_id == session_row.id)
            .where(TaskModel.state.in_(tuple(sorted(READY_TASK_STATES))))
            .where(TaskModel.updated_at <= ready_threshold)
            .order_by(TaskModel.updated_at.asc()),
        )
        if stale_task is not None:
            summary = self._trim_context_text(stale_task.summary_text or stale_task.title, HANDOFF_PREVIEW_LIMIT)
            return (
                (
                    "Curator sweep triggered because a ready task has not been claimed for too long.\n\n"
                    "Read CURRENT_STATE.md, TASK_BOARD.md, HANDOFFS.md, and the relevant TASKS/*.md card first.\n"
                    "Do not create new scope. Re-direct, requeue, open discuss, or escalate only if necessary.\n\n"
                    f"Reason: stale ready task\n"
                    f"Task: {stale_task.task_key}\n"
                    f"Assigned agent: {stale_task.assigned_agent or 'unassigned'}\n"
                    f"Context: {summary}\n"
                ),
                "stale_ready_task",
            )

        total_agents = len(session_row.agents)
        attached_agents = self._attached_agent_count(session_row.agents, now=now)
        created_at = session_row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        active_job_count = int(
            db.scalar(
                select(func.count())
                .select_from(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status == "in_progress"),
            )
            or 0,
        )
        if (
            session_row.status in {"waiting_for_workers", "launching", "restarting_workers"}
            and 0 < attached_agents < total_agents
            and created_at <= now - timedelta(seconds=CURATION_ATTACHMENT_MISMATCH_SECONDS)
        ):
            return (
                (
                    "Curator sweep triggered because some workers are alive but the session is still not fully attached.\n\n"
                    "Read CURRENT_STATE.md, TASK_BOARD.md, HANDOFFS.md, and the recent thread delta first.\n"
                    "Do not create new scope. Diagnose the attachment mismatch, direct an existing agent, open discuss, or escalate.\n\n"
                    f"Reason: partial worker attachment\n"
                    f"Attached workers: {attached_agents}/{total_agents}\n"
                    f"Session status: {session_row.status}\n"
                    f"Active jobs: {active_job_count}\n"
                ),
                "partial_attachment",
            )

        planner_agent = self._find_planner_agent(session_row.agents)
        if planner_agent is None:
            return None
        active_planner_job = db.scalar(
            select(JobModel)
            .where(JobModel.session_id == session_row.id)
            .where(JobModel.agent_name == planner_agent.agent_name)
            .where(JobModel.status == "in_progress")
            .order_by(JobModel.claimed_at.asc()),
        )
        if active_planner_job is None or active_planner_job.claimed_at is None:
            return None
        planner_running_for = (now - active_planner_job.claimed_at).total_seconds()
        if planner_running_for < CURATION_PLANNER_IMBALANCE_SECONDS:
            return None
        idle_agents = [
            candidate.agent_name
            for candidate in session_row.agents
            if candidate.agent_name != planner_agent.agent_name and candidate.agent_name != "curator" and candidate.status == "idle"
        ]
        if not idle_agents:
            return None
        queued_count = int(
            db.scalar(
                select(func.count())
                .select_from(HandoffModel)
                .where(HandoffModel.session_id == session_row.id)
                .where(HandoffModel.state == "queued"),
            )
            or 0,
        )
        ready_count = int(
            db.scalar(
                select(func.count())
                .select_from(TaskModel)
                .where(TaskModel.session_id == session_row.id)
                .where(TaskModel.state.in_(tuple(sorted(READY_TASK_STATES)))),
            )
            or 0,
        )
        if queued_count == 0 and ready_count == 0:
            return None
        return (
            (
                "Curator sweep triggered because planner is still busy while other agents are idle and actionable work exists.\n\n"
                "Read CURRENT_STATE.md, TASK_BOARD.md, HANDOFFS.md, and the latest task cards first.\n"
                "Do not create new scope. Re-direct, requeue, open discuss, or escalate only if necessary.\n\n"
                f"Reason: planner bottleneck\n"
                f"Planner job type: {active_planner_job.job_type}\n"
                f"Planner running for: {int(planner_running_for)}s\n"
                f"Idle agents: {', '.join(idle_agents)}\n"
                f"Ready tasks: {ready_count}\n"
                f"Queued handoffs: {queued_count}\n"
            ),
            "planner_bottleneck",
        )

    def _queue_curator_event(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        source_agent: str,
        source_job: JobModel,
        reason: str,
        detail_lines: list[str],
    ) -> bool:
        curator_agent = self._find_curator_agent(session_row.agents)
        if curator_agent is None or source_agent.lower() == curator_agent.agent_name.lower():
            return False
        existing_job = db.scalar(
            select(JobModel)
            .where(JobModel.session_id == session_row.id)
            .where(JobModel.agent_name == curator_agent.agent_name)
            .where(JobModel.job_type.in_(["curation_event", "curation_sweep"]))
            .where(JobModel.status.in_(["pending", "in_progress"])),
        )
        if existing_job is not None:
            return False
        trimmed_details = detail_lines[:4]
        prompt = self._build_curator_event_prompt(
            reason=reason,
            source_agent=source_agent,
            detail_lines=trimmed_details,
        )
        db.add(
            JobModel(
                session_id=session_row.id,
                agent_name=curator_agent.agent_name,
                job_type="curation_event",
                source_discord_message_id=source_job.source_discord_message_id,
                user_id=f"agent:{source_agent}",
                input_text=prompt,
                status="pending",
                session_epoch=session_row.session_epoch,
                idempotency_key=f"curation_event:{session_row.id}:{session_row.session_epoch}:{reason}:{source_job.id}",
            ),
        )
        self.transcript_service.add_entry(
            db,
            session_id=session_row.id,
            direction="system",
            actor="bridge",
            content=(
                f"Curator event queued after {reason}: "
                f"{self._trim_context_text(' | '.join(trimmed_details), HANDOFF_PREVIEW_LIMIT)}"
            ),
            source_discord_message_id=source_job.source_discord_message_id,
        )
        return True

    def _build_curator_event_prompt(
        self,
        *,
        reason: str,
        source_agent: str,
        detail_lines: list[str],
    ) -> str:
        rendered_details = "\n".join(f"- {item}" for item in detail_lines) if detail_lines else "- no extra details"
        return (
            f"Curator event triggered after `{source_agent}` updated the shared flow.\n\n"
            "Read CURRENT_STATE.md, TASK_BOARD.md, HANDOFFS.md, and the recent thread delta first.\n"
            "Do not create new scope. Decide whether an existing task should be directed, requeued, discussed, or escalated.\n\n"
            f"Reason: {reason}\n"
            "Recent signals:\n"
            f"{rendered_details}\n"
        )

    @staticmethod
    def _job_type_for_task(task: TaskModel) -> str:
        if task.role == "verification":
            return "verification"
        return "handoff"

    def _can_self_claim_task(self, db: Session, *, session_row: SessionModel, task: TaskModel) -> bool:
        active_tasks = list(
            db.scalars(
                select(TaskModel)
                .where(TaskModel.session_id == session_row.id)
                .where(TaskModel.state == "in_progress")
                .where(TaskModel.id != task.id),
            ),
        )
        candidate_scope = self._parse_scope_list(task.file_scope_json)
        for active in active_tasks:
            if task.semantic_scope and active.semantic_scope and task.semantic_scope == active.semantic_scope:
                return False
            active_scope = self._parse_scope_list(active.file_scope_json)
            if candidate_scope and active_scope and candidate_scope.intersection(active_scope):
                return False
        return True

    @staticmethod
    def _parse_scope_list(raw_scope: str | None) -> set[str]:
        if not raw_scope:
            return set()
        try:
            payload = json.loads(raw_scope)
        except Exception:  # noqa: BLE001
            return set()
        if not isinstance(payload, list):
            return set()
        return {str(item).strip() for item in payload if str(item).strip()}

    @staticmethod
    def _extract_prefixed_line(text: str, prefix: str) -> str | None:
        for raw_line in text.splitlines():
            if raw_line.strip().lower().startswith(prefix.lower()):
                return raw_line.split(":", 1)[1].strip() if ":" in raw_line else raw_line.strip()
        return None

    @staticmethod
    def _normalize_task_payload_text(text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if any(
            raw_line.startswith(prefix)
            for raw_line in lines
            for prefix in ("Target summary:", "Files:", "Done condition:")
        ):
            return stripped
        if any(raw_line.startswith(("OPS:", "HUMAN:", "ANSWER:", "ISSUE:", "DONE:")) for raw_line in lines):
            preferred: list[str] = []
            for prefix in ("ANSWER:", "HUMAN:"):
                preferred.extend(
                    raw_line[len(prefix) :].strip()
                    for raw_line in lines
                    if raw_line.startswith(prefix) and raw_line[len(prefix) :].strip()
                )
            issue_lines = [
                raw_line[len("ISSUE:") :].strip()
                for raw_line in lines
                if raw_line.startswith("ISSUE:") and raw_line[len("ISSUE:") :].strip()
            ]
            preferred.extend(f"Issue: {item}" for item in issue_lines)
            if preferred:
                return "\n".join(preferred)
            non_ops = [raw_line for raw_line in lines if not raw_line.startswith("OPS:")]
            if non_ops:
                return "\n".join(non_ops)
        return stripped

    @staticmethod
    def _derive_task_title_from_handoff(body: str) -> str:
        normalized_body = SessionService._normalize_task_payload_text(body)
        summary_line = SessionService._extract_prefixed_line(normalized_body, "Target summary:")
        first_line = next((line.strip() for line in normalized_body.splitlines() if line.strip()), "Focused follow-up task")
        candidate = summary_line or first_line
        candidate = TASK_CARD_ID_RE.sub("", candidate).strip(" -:\t")
        if not candidate:
            candidate = "Focused follow-up task"
        return candidate[:96]

    @staticmethod
    def _derive_semantic_scope(text: str) -> str | None:
        normalized = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return normalized[:64] or None

    @staticmethod
    def _validate_job_concurrency(
        *,
        job: JobModel,
        session_row: SessionModel,
        lease_token: str | None,
        task_revision: int | None,
        session_epoch: int | None,
    ) -> bool:
        if session_epoch is not None and job.session_epoch != session_epoch:
            return False
        if job.session_epoch != session_row.session_epoch:
            return False
        if job.lease_token and lease_token is not None and job.lease_token != lease_token:
            return False
        if job.task_revision and task_revision is not None and job.task_revision != task_revision:
            return False
        return True

    def _synchronize_completed_task(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        job: JobModel,
        actor: str,
        report_text: str,
        question_present: bool,
        handoffs: list[HandoffRequest],
    ) -> None:
        if not job.task_id:
            return
        task = db.scalar(select(TaskModel).where(TaskModel.id == job.task_id))
        if task is None:
            return

        same_task_handoff = any(
            (handoff.task_id or "").strip().upper() == task.task_key.upper()
            for handoff in handoffs
        )
        if same_task_handoff:
            next_state = task.state if task.state in READY_TASK_STATES else "ready"
        elif question_present:
            next_state = "blocked_on_operator"
        else:
            next_state = "done"

        task.state = next_state
        if not same_task_handoff:
            task.revision += 1
        task.current_lease_token = None
        task.current_worker_id = None
        task.latest_log_name = None
        task.updated_at = utcnow()
        task.last_transition_at = utcnow()
        if report_text:
            task.summary_text = self._trim_context_text(report_text, SESSION_SUMMARY_TEXT_LIMIT)

        handoff = None
        if job.handoff_id:
            handoff = db.scalar(select(HandoffModel).where(HandoffModel.id == job.handoff_id))
            if handoff is not None and handoff.state not in TERMINAL_HANDOFF_STATES:
                handoff.state = "consumed"
                handoff.consumed_at = utcnow()
                handoff.updated_at = utcnow()

        self._append_task_event(
            db,
            session_id=session_row.id,
            task=task,
            handoff=handoff,
            event_type="task_completed",
            actor=actor,
            payload={
                "state": next_state,
                "revision": task.revision,
                "handoff_count": len(handoffs),
            },
        )

    def _synchronize_failed_task(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        job: JobModel,
        actor: str,
        error_text: str,
    ) -> None:
        if not job.task_id:
            return
        task = db.scalar(select(TaskModel).where(TaskModel.id == job.task_id))
        if task is None:
            return

        task.state = "failed"
        task.revision += 1
        task.current_lease_token = None
        task.current_worker_id = None
        task.updated_at = utcnow()
        task.last_transition_at = utcnow()
        task.summary_text = self._trim_context_text(error_text, SESSION_SUMMARY_TEXT_LIMIT)

        handoff = None
        if job.handoff_id:
            handoff = db.scalar(select(HandoffModel).where(HandoffModel.id == job.handoff_id))
            if handoff is not None and handoff.state not in TERMINAL_HANDOFF_STATES:
                handoff.state = "failed"
                handoff.updated_at = utcnow()

        self._append_task_event(
            db,
            session_id=session_row.id,
            task=task,
            handoff=handoff,
            event_type="task_failed",
            actor=actor,
            payload={"state": "failed", "revision": task.revision},
        )

    def _queue_discussions(
        self,
        db: Session,
        *,
        session_id: str,
        source_agent: str,
        source_job: JobModel,
        discussions: list[DiscussRequest],
    ) -> None:
        curator_details: list[str] = []
        for discuss in discussions:
            if discuss.discuss_type == "open":
                for target_agent in discuss.ask_agents or []:
                    db.add(
                        JobModel(
                            session_id=session_id,
                            agent_name=target_agent,
                            job_type="discussion",
                            source_discord_message_id=source_job.source_discord_message_id,
                            user_id=f"agent:{source_agent}",
                            input_text=self._build_discussion_prompt(
                                source_agent=source_agent,
                                target_agent=target_agent,
                                discuss=discuss,
                            ),
                            status="pending",
                            session_epoch=source_job.session_epoch,
                            idempotency_key=(
                                f"discuss:{session_id}:{discuss.anomaly_id or 'none'}:"
                                f"{source_agent}:{target_agent}:{discuss.discuss_type}"
                            ),
                        ),
                    )
                    self.transcript_service.add_entry(
                        db,
                        session_id=session_id,
                        direction="system",
                        actor=source_agent,
                        content=(
                            f"Discussion queued to {target_agent}"
                            f"{f' ({discuss.anomaly_id})' if discuss.anomaly_id else ''}: "
                            f"{self._trim_context_text(discuss.body, HANDOFF_PREVIEW_LIMIT)}"
                        ),
                        source_discord_message_id=source_job.source_discord_message_id,
                    )
                    curator_details.append(
                        f"{discuss.discuss_type}:{target_agent}:{self._trim_context_text(discuss.body, HANDOFF_PREVIEW_LIMIT)}",
                    )
            elif discuss.discuss_type == "reply" and discuss.to_agent:
                db.add(
                    JobModel(
                        session_id=session_id,
                        agent_name=discuss.to_agent,
                        job_type="discussion",
                        source_discord_message_id=source_job.source_discord_message_id,
                        user_id=f"agent:{source_agent}",
                        input_text=self._build_discussion_prompt(
                            source_agent=source_agent,
                            target_agent=discuss.to_agent,
                            discuss=discuss,
                        ),
                        status="pending",
                        session_epoch=source_job.session_epoch,
                        idempotency_key=(
                            f"discuss:{session_id}:{discuss.anomaly_id or 'none'}:"
                            f"{source_agent}:{discuss.to_agent}:{discuss.discuss_type}"
                        ),
                    ),
                )
                self.transcript_service.add_entry(
                    db,
                    session_id=session_id,
                    direction="system",
                    actor=source_agent,
                    content=(
                        f"Discussion reply queued to {discuss.to_agent}"
                        f"{f' ({discuss.anomaly_id})' if discuss.anomaly_id else ''}: "
                        f"{self._trim_context_text(discuss.body, HANDOFF_PREVIEW_LIMIT)}"
                    ),
                    source_discord_message_id=source_job.source_discord_message_id,
                )
                curator_details.append(
                    f"{discuss.discuss_type}:{discuss.to_agent}:{self._trim_context_text(discuss.body, HANDOFF_PREVIEW_LIMIT)}",
                )
        if curator_details:
            self._queue_curator_event(
                db,
                session_row=self._require_session(
                    db,
                    select(SessionModel)
                    .options(selectinload(SessionModel.agents))
                    .where(SessionModel.id == session_id),
                ),
                source_agent=source_agent,
                source_job=source_job,
                reason="discussion_update",
                detail_lines=curator_details,
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
        if failed_job.job_type not in {"handoff", "verification"}:
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
                session_epoch=session_row.session_epoch,
                idempotency_key=f"recovery:{session_row.id}:{failed_job.id}",
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

    def _record_discussion_rejections(
        self,
        db: Session,
        *,
        session_id: str,
        source_agent: str,
        source_job: JobModel,
        rejections: list[str],
    ) -> None:
        for rejection in rejections:
            self.transcript_service.add_entry(
                db,
                session_id=session_id,
                direction="system",
                actor="bridge",
                content=f"Discussion update rejected from {source_agent}: {rejection}",
                source_discord_message_id=source_job.source_discord_message_id,
            )

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
                session_epoch=session_row.session_epoch,
                idempotency_key=f"handoff_repair:{session_row.id}:{source_job.id}",
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
        self._release_orchestration_job_lease(
            db=db,
            job=active_job,
            actor_id=agent.agent_name,
            status="released",
        )
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
        return body

    def _format_failure_thread_message(
        self,
        *,
        agent_name: str,
        sanitized_error: str,
        recovery_queued: bool,
        quiet_discord: bool,
    ) -> str:
        if "OPS:" in sanitized_error and "HUMAN:" in sanitized_error:
            return self._quiet_discord_text(sanitized_error) if quiet_discord else sanitized_error.strip()
        human_text = self._normalize_thread_text(sanitized_error) or "(no error details)"
        issue_text = "planner_recovery_queued" if recovery_queued else "triage_required"
        message = "\n".join(
            [
                f"OPS: type=failed | actor={agent_name} | task=none | state=failed | read=CURRENT_STATE.md | reason=bridge_failure",
                f"HUMAN: {human_text}",
                f"ISSUE: {issue_text}",
            ],
        )
        return self._quiet_discord_text(message) if quiet_discord else message

    def _quiet_discord_text(self, text: str) -> str:
        normalized_lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        if not normalized_lines:
            return ""
        trimmed_lines: list[str] = []
        visible_lines = normalized_lines[:QUIET_DISCORD_LINE_LIMIT]
        deferred_issue_lines = [
            line
            for line in normalized_lines[QUIET_DISCORD_LINE_LIMIT:]
            if line.startswith("ISSUE:")
        ]
        for line in visible_lines:
            if line.startswith("HUMAN:"):
                human_body = self._normalize_thread_text(line[len("HUMAN:") :])
                trimmed_lines.append(f"HUMAN: {human_body}" if human_body else "HUMAN:")
                continue
            if line.startswith("ISSUE:"):
                issue_body = self._normalize_thread_text(line[len("ISSUE:") :])
                trimmed_lines.append(f"ISSUE: {issue_body}" if issue_body else "ISSUE:")
                continue
            trimmed_lines.append(self._trim_thread_text(line, 180))
        for line in deferred_issue_lines:
            issue_body = self._normalize_thread_text(line[len("ISSUE:") :])
            formatted = f"ISSUE: {issue_body}" if issue_body else "ISSUE:"
            if formatted not in trimmed_lines:
                trimmed_lines.append(formatted)
        omitted_line_count = len(normalized_lines) - len(visible_lines) - len(deferred_issue_lines)
        if omitted_line_count > 0:
            trimmed_lines.append("HUMAN: 자세한 내용은 스레드 표시에서 생략되었습니다.")
        compact = "\n".join(trimmed_lines)
        if len(compact) <= QUIET_DISCORD_CHAR_LIMIT:
            return compact
        if any(line.startswith(("HUMAN:", "ISSUE:")) for line in trimmed_lines):
            return compact
        return self._trim_thread_text(compact, QUIET_DISCORD_CHAR_LIMIT)

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

    def _build_discussion_prompt(
        self,
        *,
        source_agent: str,
        target_agent: str,
        discuss: DiscussRequest,
    ) -> str:
        anomaly_text = discuss.anomaly_id or "unspecified"
        task_text = discuss.task_id or "none"
        if discuss.discuss_type == "open":
            return (
                f"Discussion requested by `{source_agent}` for anomaly `{anomaly_text}`.\n\n"
                "Read `CURRENT_STATE.md`, `TASK_BOARD.md`, and the relevant `TASKS/*.md` card first.\n"
                "Keep the thread-visible response short.\n"
                f"Reply to `{source_agent}` with `[[discuss type=\"reply\" to=\"{source_agent}\" anomaly=\"{anomaly_text}\"]]...[[/discuss]]`.\n\n"
                f"Current task reference: `{task_text}`\n"
                f"Target agent: `{target_agent}`\n"
                f"Discussion request:\n{discuss.body}\n"
            )
        return (
            f"Discussion reply received from `{source_agent}` for anomaly `{anomaly_text}`.\n\n"
            "Read `CURRENT_STATE.md`, `TASK_BOARD.md`, and the relevant `TASKS/*.md` card first.\n"
            "Decide the next action. If the anomaly is resolved, emit `[[discuss type=\"resolve\" ...]]`. "
            "If it still needs broader attention, emit `[[discuss type=\"escalate\" ...]]` or hand work off explicitly.\n\n"
            f"Current task reference: `{task_text}`\n"
            f"Discussion reply:\n{discuss.body}\n"
        )

    def _parse_discuss_attrs(self, attrs_text: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(['\"])(.*?)\2", attrs_text):
            attrs[match.group(1).strip().lower()] = match.group(3).strip()
        return attrs

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

    def _close_session_row(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        closed_by: str,
        transcript_content: str,
        job_error_text: str,
    ) -> None:
        session_row.status = "closed"
        session_row.desired_status = "closed"
        session_row.closed_at = utcnow()

        for agent in session_row.agents:
            agent.status = "offline"
            agent.desired_status = "closed"
            self._upsert_orchestration_presence(
                db=db,
                session_id=session_row.id,
                actor_id=agent.agent_name,
                status="offline",
                ttl_seconds=60,
            )

        for job in db.scalars(
            select(JobModel)
            .where(JobModel.session_id == session_row.id)
            .where(JobModel.status.in_(["pending", "in_progress"])),
        ):
            self._release_orchestration_job_lease(
                db=db,
                job=job,
                actor_id=job.agent_name,
                status="released",
            )
            job.status = "cancelled"
            job.completed_at = utcnow()
            job.error_text = job_error_text

        self.transcript_service.add_entry(
            db,
            session_id=session_row.id,
            direction="system",
            actor=closed_by,
            content=transcript_content,
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

    @staticmethod
    def _find_curator_agent(agents: Iterable[AgentModel]) -> AgentModel | None:
        agent_list = list(agents)
        for agent in agent_list:
            if agent.agent_name.lower() == "curator":
                return agent
        for agent in agent_list:
            role = agent.role.lower()
            if "coordination" in role or "curat" in role:
                return agent
        return None

    def _is_curator_agent(self, agent: AgentModel) -> bool:
        if agent.agent_name.lower() == "curator":
            return True
        role = agent.role.lower()
        return "coordination" in role or "curat" in role

    def _has_busy_agent(self, agents: Iterable[AgentModel]) -> bool:
        return any(agent.status == "busy" for agent in agents)

    def _has_non_curator_busy_agent(self, agents: Iterable[AgentModel]) -> bool:
        return any(agent.status == "busy" and not self._is_curator_agent(agent) for agent in agents)

    @staticmethod
    def _agent_has_attachment_signal(agent: AgentModel, *, now: datetime | None = None) -> bool:
        if agent.worker_id:
            return True
        heartbeat_at = agent.last_heartbeat_at
        if heartbeat_at is None:
            return False
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        effective_now = now or utcnow()
        return heartbeat_at + timedelta(seconds=ATTACHMENT_SIGNAL_GRACE_SECONDS) >= effective_now

    def _attached_agent_count(self, agents: Iterable[AgentModel], *, now: datetime | None = None) -> int:
        effective_now = now or utcnow()
        return sum(1 for agent in agents if self._agent_has_attachment_signal(agent, now=effective_now))

    def _refresh_session_startup_state(
        self,
        db: Session,
        *,
        session_row: SessionModel,
        now: datetime,
        previous_status: str | None,
    ) -> tuple[bool, str]:
        if session_row.closed_at is not None:
            return False, ""
        if session_row.desired_status == "paused":
            session_row.status = "paused"
            return False, ""

        total_agents = len(session_row.agents)
        if total_agents <= 0:
            session_row.status = "ready"
            return False, ""

        attached_agents = self._attached_agent_count(session_row.agents, now=now)
        active_or_pending_jobs = int(
            db.scalar(
                select(func.count())
                .select_from(JobModel)
                .where(JobModel.session_id == session_row.id)
                .where(JobModel.status.in_(["pending", "in_progress"])),
            )
            or 0,
        )

        if attached_agents >= total_agents:
            session_row.status = "resuming_jobs" if active_or_pending_jobs else "ready"
            should_send_ready = (
                session_row.status == "ready"
                and previous_status != "ready"
                and session_row.send_ready_message
                and session_row.desired_status != "paused"
            )
            ready_message = self._format_ready_message(session_row.agents) if should_send_ready else ""
            return should_send_ready, ready_message

        if attached_agents > 0:
            session_row.status = "launching"
            return False, ""

        session_row.status = "waiting_for_workers"
        return False, ""

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

    @staticmethod
    def _trim_thread_text(text: str, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        marker = " [truncated]"
        if limit <= len(marker):
            return normalized[:limit]
        return normalized[: limit - len(marker)].rstrip() + marker

    @staticmethod
    def _normalize_thread_text(text: str) -> str:
        return " ".join(text.split())

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
