from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import require_bridge_token
from ..schemas import (
    JobCompleteRequest,
    JobFailRequest,
    ThreadDeltaRequest,
    ThreadDeltaResponse,
    WorkerPollRequest,
    WorkerPollResponse,
    WorkerRegisterRequest,
    WorkerHeartbeatRequest,
)

router = APIRouter(
    prefix="/api/workers",
    tags=["workers"],
    dependencies=[Depends(require_bridge_token)],
)


@router.post("/register")
async def register_worker(payload: WorkerRegisterRequest, request: Request) -> dict[str, str]:
    services = request.app.state.services
    await services.session_service.register_worker(
        session_id=payload.session_id,
        agent_name=payload.agent_name,
        worker_id=payload.worker_id,
        pid_hint=payload.pid_hint,
    )
    return {"status": "registered"}


@router.post("/heartbeat")
async def heartbeat(payload: WorkerHeartbeatRequest, request: Request) -> dict[str, str]:
    services = request.app.state.services
    await services.session_service.heartbeat(
        session_id=payload.session_id,
        agent_name=payload.agent_name,
        worker_id=payload.worker_id,
        status=payload.status,
        pid_hint=payload.pid_hint,
        artifact_snapshot=payload.artifact_snapshot,
        activity_line=payload.activity_line,
    )
    return {"status": "ok"}


@router.post("/next-job", response_model=WorkerPollResponse)
async def next_job(payload: WorkerPollRequest, request: Request) -> WorkerPollResponse:
    services = request.app.state.services
    job = await services.session_service.claim_next_job(
        session_id=payload.session_id,
        agent_name=payload.agent_name,
        worker_id=payload.worker_id,
    )
    return WorkerPollResponse(job=job)


@router.post("/jobs/{job_id}/complete")
async def complete_job(job_id: str, payload: JobCompleteRequest, request: Request) -> dict[str, str]:
    services = request.app.state.services
    try:
        await services.session_service.complete_job(
            job_id=job_id,
            session_id=payload.session_id,
            agent_name=payload.agent_name,
            worker_id=payload.worker_id,
            output_text=payload.output_text,
            thread_output_text=payload.thread_output_text,
            lease_token=payload.lease_token,
            task_revision=payload.task_revision,
            session_epoch=payload.session_epoch,
            pid_hint=payload.pid_hint,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return {"status": "completed"}


@router.post("/jobs/{job_id}/fail")
async def fail_job(job_id: str, payload: JobFailRequest, request: Request) -> dict[str, str]:
    services = request.app.state.services
    try:
        await services.session_service.fail_job(
            job_id=job_id,
            session_id=payload.session_id,
            agent_name=payload.agent_name,
            worker_id=payload.worker_id,
            error_text=payload.error_text,
            lease_token=payload.lease_token,
            task_revision=payload.task_revision,
            session_epoch=payload.session_epoch,
            pid_hint=payload.pid_hint,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return {"status": "failed"}


@router.post("/thread-delta", response_model=ThreadDeltaResponse)
async def thread_delta(payload: ThreadDeltaRequest, request: Request) -> ThreadDeltaResponse:
    services = request.app.state.services
    return services.session_service.get_thread_delta(
        session_id=payload.session_id,
        agent_name=payload.agent_name,
        cursor=payload.cursor,
        kinds=payload.kinds,
        task_id=payload.task_id,
        limit=payload.limit,
    )
