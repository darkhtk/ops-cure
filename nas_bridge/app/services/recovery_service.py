from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..capabilities.execution.base import ExecutionProvider, ExecutionTarget
from ..capabilities.power.base import PowerProvider, PowerTarget
from ..db import session_scope
from ..models import (
    AgentModel,
    ExecutionTargetModel,
    JobModel,
    PowerTargetModel,
    SessionModel,
    SessionOperationModel,
)
from ..thread_manager import ThreadManager
from ..transcript_service import TranscriptService
from ..worker_registry import WorkerRegistry

LOGGER = logging.getLogger(__name__)
STARTUP_SESSION_STATUSES = {
    "requested",
    "waking_execution_plane",
    "awaiting_launcher",
    "waiting_for_workers",
    "launching",
    "restarting_workers",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RecoveryService:
    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        transcript_service: TranscriptService,
        thread_manager: ThreadManager,
        announcement_service,
        power_provider: PowerProvider,
        execution_provider: ExecutionProvider,
        worker_stale_after_seconds: int,
        stalled_start_timeout_seconds: int,
    ) -> None:
        self.registry = registry
        self.transcript_service = transcript_service
        self.thread_manager = thread_manager
        self.announcement_service = announcement_service
        self.power_provider = power_provider
        self.execution_provider = execution_provider
        self.worker_stale_after = timedelta(seconds=worker_stale_after_seconds)
        self.stalled_start_timeout = timedelta(seconds=stalled_start_timeout_seconds)
        self._stop_event = asyncio.Event()

    async def run_forever(self, *, interval_seconds: float = 5.0) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            try:
                await self.recover_open_sessions(reason="background-loop")
            except Exception:  # noqa: BLE001
                LOGGER.exception("Recovery loop tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop_event.set()

    async def recover_open_sessions(self, *, reason: str) -> None:
        with session_scope() as db:
            session_ids = list(
                db.scalars(
                    select(SessionModel.id).where(SessionModel.closed_at.is_(None)),
                ),
            )
        for session_id in session_ids:
            await self.recover_session(session_id=session_id, reason=reason)

    async def recover_session(
        self,
        *,
        session_id: str,
        reason: str,
        requested_by: str = "system",
        wake_if_needed: bool = False,
    ) -> None:
        cleanup_thread_id: str | None = None
        cleanup_reason: str | None = None
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            if session_row is None or session_row.closed_at is not None:
                return

            session_row.last_recovery_at = utcnow()
            session_row.last_recovery_reason = reason

            thread_state = await self.thread_manager.probe_thread_state(session_row.discord_thread_id)
            if thread_state == "missing":
                self._close_missing_thread_session(
                    db,
                    session_row=session_row,
                    reason=reason,
                )
                return

            manifest = self.registry.get_project(session_row.preset or "")

            power_target = self._load_power_target(db, session_row)
            execution_target = self._load_execution_target(db, session_row)

            if power_target is not None:
                power_status = self.power_provider.status(power_target)
                session_row.power_state = power_status.state
            else:
                session_row.power_state = "unknown"

            execution_status = None
            if execution_target is not None and session_row.preset:
                execution_status = self.execution_provider.status_for_project(
                    project_name=session_row.preset,
                    target=execution_target,
                )
                session_row.execution_state = execution_status.state
                if execution_status.launcher_id:
                    session_row.launcher_id = execution_status.launcher_id
            else:
                session_row.execution_state = "unknown"

            if session_row.desired_status == "paused":
                session_row.status = "paused"
                for agent in session_row.agents:
                    agent.desired_status = "paused"
                    if agent.status != "busy":
                        agent.status = "paused"
                self._complete_operations(db, session_row.id, "pause")
                return

            self._clear_stale_workers(session_row.agents)

            if self._is_stalled_start(session_row):
                cleanup_thread_id, cleanup_reason = self._fail_stalled_start(
                    db,
                    session_row=session_row,
                    reason=reason,
                )
            else:
                if execution_status is None or execution_status.state in {"offline", "awaiting_launcher"}:
                    if wake_if_needed and power_target is not None:
                        wake_result = self.power_provider.wake(power_target)
                        session_row.power_state = wake_result.state
                        session_row.status = "waking_execution_plane"
                    else:
                        session_row.status = "awaiting_launcher"
                    return

                if any(agent.worker_id is None for agent in session_row.agents):
                    session_row.status = "waiting_for_workers"
                    for agent in session_row.agents:
                        agent.desired_status = "ready"
                        if agent.worker_id is None:
                            agent.status = "starting"
                    self._complete_operations(db, session_row.id, "start")
                    return

                pending_jobs = list(
                    db.scalars(
                        select(JobModel)
                        .where(JobModel.session_id == session_row.id)
                        .where(JobModel.status.in_(["pending", "in_progress"])),
                    ),
                )
                for agent in session_row.agents:
                    agent.desired_status = "ready"
                    if agent.status == "paused":
                        agent.status = "idle"
                        agent.paused_reason = None

                session_row.status = "resuming_jobs" if pending_jobs else "ready"
                self._complete_operations(db, session_row.id, "start")
                self._complete_operations(db, session_row.id, "resume")
        if cleanup_thread_id is not None and cleanup_reason is not None:
            await self.thread_manager.cleanup_thread(cleanup_thread_id, cleanup_reason)
        if cleanup_thread_id is None:
            await self.announcement_service.sync_session_status(session_id)

    def _clear_stale_workers(self, agents: list[AgentModel]) -> None:
        now = utcnow()
        for agent in agents:
            heartbeat_at = ensure_aware_utc(agent.last_heartbeat_at)
            if heartbeat_at is None:
                agent.worker_id = None
                if agent.status != "busy":
                    agent.status = "starting"
                continue
            if heartbeat_at + self.worker_stale_after < now:
                agent.worker_id = None
                agent.pid_hint = None
                if agent.status != "busy":
                    agent.status = "starting"
    def _is_stalled_start(self, session_row: SessionModel) -> bool:
        if session_row.status not in STARTUP_SESSION_STATUSES:
            return False
        if session_row.closed_at is not None:
            return False
        created_at = ensure_aware_utc(session_row.created_at)
        if created_at is None:
            return False
        if created_at + self.stalled_start_timeout > utcnow():
            return False
        if any(agent.worker_id for agent in session_row.agents):
            return False
        return True

    def _fail_stalled_start(
        self,
        db,
        *,
        session_row: SessionModel,
        reason: str,
    ) -> tuple[str, str]:
        session_row.status = "failed_start"
        session_row.desired_status = "closed"
        session_row.execution_state = "stalled_start"
        session_row.closed_at = utcnow()
        session_row.pause_reason = "Startup timed out before any worker attached."
        for agent in session_row.agents:
            agent.worker_id = None
            agent.pid_hint = None
            agent.status = "offline"
            agent.desired_status = "closed"
            agent.paused_reason = "Startup timed out."
        for job in db.scalars(
            select(JobModel)
            .where(JobModel.session_id == session_row.id)
            .where(JobModel.status.in_(["pending", "in_progress"])),
        ):
            job.status = "cancelled"
            job.completed_at = utcnow()
            if not job.error_text:
                job.error_text = "Cancelled because the session startup timed out."
        self.transcript_service.add_entry(
            db,
            session_id=session_row.id,
            direction="system",
            actor="bridge",
            content=(
                f"Startup timed out with no workers attached after {int(self.stalled_start_timeout.total_seconds())} seconds. "
                f"Session marked failed_start during {reason} recovery."
            ),
        )
        self._fail_operations(
            db,
            session_id=session_row.id,
            operation_type="start",
            result_json=json.dumps(
                {
                    "reason": "stalled_start_timeout",
                    "timeout_seconds": int(self.stalled_start_timeout.total_seconds()),
                },
                ensure_ascii=False,
            ),
        )
        return session_row.discord_thread_id, "Ops-Cure cleaned up a stalled startup session."

    def _close_missing_thread_session(
        self,
        db,
        *,
        session_row: SessionModel,
        reason: str,
    ) -> None:
        session_row.status = "closed"
        session_row.desired_status = "closed"
        session_row.execution_state = "thread_missing"
        session_row.closed_at = utcnow()
        session_row.pause_reason = "Discord thread was deleted; session closed automatically."
        for agent in session_row.agents:
            agent.worker_id = None
            agent.pid_hint = None
            agent.status = "offline"
            agent.desired_status = "closed"
            agent.paused_reason = "Discord thread deleted; session closed."
        for job in db.scalars(
            select(JobModel)
            .where(JobModel.session_id == session_row.id)
            .where(JobModel.status.in_(["pending", "in_progress"])),
        ):
            job.status = "cancelled"
            job.completed_at = utcnow()
            if not job.error_text:
                job.error_text = "Cancelled because the Discord thread was deleted."
        self.transcript_service.add_entry(
            db,
            session_id=session_row.id,
            direction="system",
            actor="bridge",
            content=(
                "Discord thread is missing. Ops-Cure automatically closed the session "
                f"during {reason} recovery."
            ),
        )
        for operation_type in ("start", "resume", "pause"):
            self._fail_operations(
                db,
                session_id=session_row.id,
                operation_type=operation_type,
                result_json=json.dumps(
                    {
                        "reason": "discord_thread_missing",
                        "recovery_reason": reason,
                    },
                    ensure_ascii=False,
                ),
            )

    @staticmethod
    def _load_power_target(db, session_row: SessionModel) -> PowerTarget | None:
        if not session_row.power_target_name:
            return None
        row = db.scalar(
            select(PowerTargetModel).where(PowerTargetModel.name == session_row.power_target_name),
        )
        if row is None:
            return None
        metadata = {}
        if row.metadata_json:
            metadata = json.loads(row.metadata_json)
        return PowerTarget(
            name=row.name,
            provider=row.provider,
            mac_address=row.mac_address,
            broadcast_ip=row.broadcast_ip,
            metadata=metadata,
        )

    @staticmethod
    def _load_execution_target(db, session_row: SessionModel) -> ExecutionTarget | None:
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
        return ExecutionTarget(
            name=row.name,
            provider=row.provider,
            platform=row.platform,
            launcher_id_hint=row.launcher_id_hint,
            host_pattern=row.host_pattern,
            auto_start_expected=metadata.get("auto_start_expected", True),
            metadata=metadata,
        )

    @staticmethod
    def _complete_operations(db, session_id: str, operation_type: str) -> None:
        operations = list(
            db.scalars(
                select(SessionOperationModel)
                .where(SessionOperationModel.session_id == session_id)
                .where(SessionOperationModel.operation_type == operation_type)
                .where(SessionOperationModel.status.in_(["pending", "running"])),
            ),
        )
        for operation in operations:
            operation.status = "completed"
            operation.completed_at = utcnow()

    @staticmethod
    def _fail_operations(db, session_id: str, operation_type: str, result_json: str | None = None) -> None:
        operations = list(
            db.scalars(
                select(SessionOperationModel)
                .where(SessionOperationModel.session_id == session_id)
                .where(SessionOperationModel.operation_type == operation_type)
                .where(SessionOperationModel.status.in_(["pending", "running"])),
            ),
        )
        for operation in operations:
            operation.status = "failed"
            operation.result_json = result_json
            operation.completed_at = utcnow()
