from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_bridge_token
from ..schemas import (
    VerifyClaimRequest,
    VerifyRunClaimResponse,
    VerifyRunCompleteRequest,
    VerifyRunSummaryResponse,
)

router = APIRouter(
    prefix="/api/verification",
    tags=["verification"],
    dependencies=[Depends(require_bridge_token)],
)


@router.post("/claim", response_model=list[VerifyRunClaimResponse])
async def claim_runs(payload: VerifyClaimRequest, request: Request) -> list[VerifyRunClaimResponse]:
    services = request.app.state.services
    return await services.verification_service.claim_runs(
        launcher_id=payload.launcher_id,
        capacity=payload.capacity,
    )


@router.post("/runs/{run_id}/complete", response_model=VerifyRunSummaryResponse)
async def complete_run(
    run_id: str,
    payload: VerifyRunCompleteRequest,
    request: Request,
) -> VerifyRunSummaryResponse:
    services = request.app.state.services
    return await services.verification_service.complete_run(
        run_id=run_id,
        launcher_id=payload.launcher_id,
        status=payload.status,
        summary_text=payload.summary_text,
        error_text=payload.error_text,
        artifacts=payload.artifacts,
    )
