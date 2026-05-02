"""H5: /v2/diagnostics -- aggregate runtime stats from broker, agents,
and v2 op state distribution.

Operators hit this to see at a glance: are agents running? are events
flowing? how many ops are stuck in each state?
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from ..auth import BridgeCaller, require_bridge_caller
from ..db import session_scope
from ..kernel.v2 import V2Repository
from ..kernel.v2.models import OperationV2Model

router = APIRouter(prefix="/v2/diagnostics", tags=["v2-diagnostics"])


@router.get("")
def diagnostics(
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    services = request.app.state.services
    broker = services.subscription_broker

    # broker backlog sizes per space (cap to prevent huge response)
    backlog_summary: list[dict[str, Any]] = []
    with broker._lock:  # noqa: SLF001 -- diagnostic peek, no mutation
        for space_id, deque_obj in list(broker._backlog.items())[:200]:
            backlog_summary.append({
                "space_id": space_id,
                "size": len(deque_obj),
            })

    # agent runner snapshots
    agent_summary: list[dict[str, Any]] = []
    agent_service = getattr(services, "agent_service", None)
    if agent_service is not None:
        for runner in getattr(agent_service, "_runners", []):
            agent_summary.append({
                "actor_handle": runner.actor_handle,
                "metrics": runner.metrics,
            })

    # v2 op state distribution
    state_distribution: dict[str, int] = {}
    repo = V2Repository()
    with session_scope() as db:
        rows = db.execute(
            select(OperationV2Model.state, func.count(OperationV2Model.id))
            .group_by(OperationV2Model.state)
        ).all()
        for state, count in rows:
            state_distribution[state] = int(count)

        kind_distribution: dict[str, int] = {}
        rows = db.execute(
            select(OperationV2Model.kind, func.count(OperationV2Model.id))
            .group_by(OperationV2Model.kind)
        ).all()
        for kind, count in rows:
            kind_distribution[kind] = int(count)

    return {
        "broker": {
            "spaces": len(backlog_summary),
            "backlogs": backlog_summary,
        },
        "agents": agent_summary,
        "operations": {
            "total": sum(state_distribution.values()),
            "by_state": state_distribution,
            "by_kind": kind_distribution,
        },
    }
