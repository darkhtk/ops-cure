from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_bridge_token
from ..schemas import (
    CatalogRegistrationRequest,
    LaunchClaimRequest,
    ProjectFindClaimRequest,
    ProjectFindCompleteRequest,
    ProjectFindLaunchResponse,
    ProjectFindSummaryResponse,
    SessionLaunchResponse,
    SessionSummaryResponse,
)

router = APIRouter(
    prefix="/api/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_bridge_token)],
)


@router.post("/projects/register")
async def register_projects(payload: CatalogRegistrationRequest, request: Request) -> dict[str, int | str]:
    services = request.app.state.services
    services.registry.register_projects(payload.launcher_id, payload.hostname, payload.projects)
    return {
        "status": "registered",
        "launchers": services.registry.active_launcher_count(),
        "projects": services.registry.tracked_project_count(),
    }


@router.post("/launches/claim", response_model=list[SessionLaunchResponse])
async def claim_launches(payload: LaunchClaimRequest, request: Request) -> list[SessionLaunchResponse]:
    services = request.app.state.services
    return await services.session_service.claim_launches(payload.launcher_id, payload.capacity)


@router.post("/project-finds/claim", response_model=list[ProjectFindLaunchResponse])
async def claim_project_finds(payload: ProjectFindClaimRequest, request: Request) -> list[ProjectFindLaunchResponse]:
    services = request.app.state.services
    return await services.session_service.claim_project_finds(payload.launcher_id, payload.capacity)


@router.post("/project-finds/{find_id}/complete", response_model=ProjectFindSummaryResponse)
async def complete_project_find(
    find_id: str,
    payload: ProjectFindCompleteRequest,
    request: Request,
) -> ProjectFindSummaryResponse:
    services = request.app.state.services
    return await services.session_service.complete_project_find(
        find_id=find_id,
        launcher_id=payload.launcher_id,
        status=payload.status,
        selected_path=payload.selected_path,
        selected_name=payload.selected_name,
        reason=payload.reason,
        confidence=payload.confidence,
        candidates=payload.candidates,
        error_text=payload.error_text,
    )


@router.get("/project-finds/{find_id}", response_model=ProjectFindSummaryResponse)
async def get_project_find(find_id: str, request: Request) -> ProjectFindSummaryResponse:
    services = request.app.state.services
    return await services.session_service.get_project_find(find_id)


@router.get("/{session_id}", response_model=SessionSummaryResponse)
async def get_session(session_id: str, request: Request) -> SessionSummaryResponse:
    services = request.app.state.services
    return await services.session_service.get_session_summary(session_id)
