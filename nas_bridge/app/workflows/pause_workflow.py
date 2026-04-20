from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import session_scope
from ..models import JobModel, SessionModel, SessionOperationModel
from ..schemas import SessionPauseResponse
from ..services.recovery_service import RecoveryService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PauseWorkflow:
    def __init__(self, *, recovery_service: RecoveryService, transcript_service) -> None:
        self.recovery_service = recovery_service
        self.transcript_service = transcript_service

    async def pause(self, *, session_id: str, requested_by: str, reason: str | None = None) -> SessionPauseResponse:
        pause_reason = reason or f"Paused by {requested_by}"
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            if session_row is None:
                raise ValueError("Session not found.")
            session_row.desired_status = "paused"
            session_row.status = "paused"
            session_row.pause_reason = pause_reason
            for agent in session_row.agents:
                agent.desired_status = "paused"
                agent.paused_reason = pause_reason
                if agent.status != "busy":
                    agent.status = "paused"
            for job in db.scalars(
                select(JobModel)
                .where(JobModel.session_id == session_id)
                .where(JobModel.status == "pending"),
            ):
                job.status = "cancelled"
                job.completed_at = utcnow()
                job.error_text = "Cancelled while session was paused."
            db.add(
                SessionOperationModel(
                    session_id=session_row.id,
                    operation_type="pause",
                    status="completed",
                    requested_by=requested_by,
                    input_json=json.dumps({"reason": pause_reason}, ensure_ascii=False),
                    completed_at=utcnow(),
                ),
            )
            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor=requested_by,
                content=f"Session paused. Reason: {pause_reason}",
            )
            return SessionPauseResponse(
                session_id=session_row.id,
                status=session_row.status,
                desired_status=session_row.desired_status,
                pause_reason=session_row.pause_reason,
            )

    async def resume(self, *, session_id: str, requested_by: str) -> SessionPauseResponse:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            if session_row is None:
                raise ValueError("Session not found.")
            session_row.desired_status = "ready"
            session_row.pause_reason = None
            for agent in session_row.agents:
                agent.desired_status = "ready"
                agent.paused_reason = None
                if agent.status == "paused":
                    agent.status = "starting"
            db.add(
                SessionOperationModel(
                    session_id=session_row.id,
                    operation_type="resume",
                    status="pending",
                    requested_by=requested_by,
                    input_json=json.dumps({}, ensure_ascii=False),
                ),
            )
            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor=requested_by,
                content="Session resume requested.",
            )
        await self.recovery_service.recover_session(
            session_id=session_id,
            reason="resume-request",
            requested_by=requested_by,
            wake_if_needed=True,
        )
        with session_scope() as db:
            session_row = db.scalar(select(SessionModel).where(SessionModel.id == session_id))
            if session_row is None:
                raise ValueError("Session not found.")
            return SessionPauseResponse(
                session_id=session_row.id,
                status=session_row.status,
                desired_status=session_row.desired_status,
                pause_reason=session_row.pause_reason,
            )
