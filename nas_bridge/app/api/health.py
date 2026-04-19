from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    services = request.app.state.services
    agents_in_drift, sessions_with_drift = services.session_service.get_drift_overview()
    return HealthResponse(
        status="ok",
        discord_enabled=services.discord_gateway.enabled,
        discord_connected=services.discord_gateway.connected,
        active_launchers=services.registry.active_launcher_count(),
        tracked_projects=services.registry.tracked_project_count(),
        agents_in_drift=agents_in_drift,
        sessions_with_drift=sessions_with_drift,
    )
