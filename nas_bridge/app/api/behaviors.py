from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_bridge_token
from ..behaviors.catalog import BehaviorSummary

router = APIRouter(
    prefix="/api/behaviors",
    tags=["behaviors"],
    dependencies=[Depends(require_bridge_token)],
)


@router.get("", response_model=list[BehaviorSummary])
async def list_behaviors(request: Request) -> list[BehaviorSummary]:
    services = request.app.state.services
    return services.behavior_catalog_service.list_behaviors()
