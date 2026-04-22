"""Workflow kernel providers for generic Space/Actor/Event views."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...kernel.actors import ActorListResponse, ActorSummary
from ...kernel.bindings import KernelBehaviorBinding
from ...kernel.events import (
    EventDeltaResponse,
    EventEnvelope,
    EventSummary,
    encode_event_cursor,
    paginate_event_envelopes,
)
from ...kernel.spaces import ActorSummary as SpaceActorSummary
from ...kernel.spaces import SpaceSummary
from ...kernel.storage import session_scope
from .models import SessionModel, TranscriptModel


class WorkflowKernelProvider:
    behavior_id = "orchestration"

    def get_space(self, *, space_id: str) -> SpaceSummary | None:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == space_id),
            )
            if session_row is None:
                return None
            return self._space_summary(session_row)

    def get_space_by_thread(self, *, thread_id: str) -> SpaceSummary | None:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.discord_thread_id == thread_id),
            )
            if session_row is None:
                return None
            return self._space_summary(session_row)

    def get_actors_for_space(self, *, space_id: str) -> ActorListResponse | None:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == space_id),
            )
            if session_row is None:
                return None
            return self._actor_response(session_row)

    def get_actors_for_thread(self, *, thread_id: str) -> ActorListResponse | None:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.discord_thread_id == thread_id),
            )
            if session_row is None:
                return None
            return self._actor_response(session_row)

    def get_events_for_space(
        self,
        *,
        space_id: str,
        after_cursor: str | None = None,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> EventDeltaResponse | None:
        with session_scope() as db:
            session_row = db.scalar(select(SessionModel).where(SessionModel.id == space_id))
            if session_row is None:
                return None
            events = list(
                db.scalars(
                    select(TranscriptModel)
                    .where(TranscriptModel.session_id == session_row.id)
                    .order_by(TranscriptModel.created_at.asc(), TranscriptModel.id.asc()),
                ),
            )
            return self._event_response(
                session_id=session_row.id,
                events=events,
                after_cursor=after_cursor,
                limit=limit,
                kinds=kinds,
            )

    def get_events_for_thread(
        self,
        *,
        thread_id: str,
        after_cursor: str | None = None,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> EventDeltaResponse | None:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel).where(SessionModel.discord_thread_id == thread_id),
            )
            if session_row is None:
                return None
            events = list(
                db.scalars(
                    select(TranscriptModel)
                    .where(TranscriptModel.session_id == session_row.id)
                    .order_by(TranscriptModel.created_at.asc(), TranscriptModel.id.asc()),
                ),
            )
            return self._event_response(
                session_id=session_row.id,
                events=events,
                after_cursor=after_cursor,
                limit=limit,
                kinds=kinds,
            )

    def _space_summary(self, session_row: SessionModel) -> SpaceSummary:
        actors = [
            SpaceActorSummary(
                name=agent.agent_name,
                kind="agent",
                status=agent.status,
                detail=agent.role,
            )
            for agent in session_row.agents
        ]
        return SpaceSummary(
            id=session_row.id,
            domain_type="orchestration",
            transport_kind="discord_thread",
            transport_address=session_row.discord_thread_id,
            title=session_row.project_name,
            status=session_row.status,
            created_at=session_row.created_at,
            updated_at=session_row.closed_at,
            actors=actors,
            metadata={
                "target_project_name": session_row.target_project_name,
                "desired_status": session_row.desired_status,
                "workdir": session_row.workdir,
                "launcher_id": session_row.launcher_id,
            },
        )

    def _actor_response(self, session_row: SessionModel) -> ActorListResponse:
        return ActorListResponse(
            space_id=session_row.id,
            domain_type="orchestration",
            actors=[
                ActorSummary(
                    name=agent.agent_name,
                    kind="agent",
                    status=agent.status,
                    detail=agent.role,
                    last_active_at=agent.last_heartbeat_at,
                )
                for agent in session_row.agents
            ],
        )

    def _event_response(
        self,
        *,
        session_id: str,
        events: list[TranscriptModel],
        after_cursor: str | None,
        limit: int,
        kinds: list[str] | None,
    ) -> EventDeltaResponse:
        return paginate_event_envelopes(
            space_id=session_id,
            domain_type="orchestration",
            items=[
                EventEnvelope(
                    cursor=encode_event_cursor(created_at=event.created_at, event_id=event.id),
                    space_id=session_id,
                    event=EventSummary(
                        id=event.id,
                        kind=event.direction,
                        actor_name=event.actor,
                        content=event.content,
                        created_at=event.created_at,
                    ),
                )
                for event in events
            ],
            after_cursor=after_cursor,
            limit=limit,
            kinds=kinds,
        )


def build_workflow_kernel_binding() -> KernelBehaviorBinding:
    provider = WorkflowKernelProvider()
    return KernelBehaviorBinding(
        behavior_id="orchestration",
        space_provider=provider,
        actor_provider=provider,
        event_provider=provider,
    )
