"""Ops kernel providers for generic Space/Actor/Event views."""

from __future__ import annotations

from sqlalchemy import select

from ...kernel.actors import ActorListResponse, ActorSummary
from ...kernel.bindings import KernelBehaviorBinding
from ...kernel.events import EventListResponse, EventSummary
from ...kernel.spaces import SpaceSummary
from ...kernel.storage import session_scope
from .models import OpsEventModel, OpsParticipantModel, OpsThreadModel


class OpsKernelProvider:
    behavior_id = "ops"

    def get_space(self, *, space_id: str) -> SpaceSummary | None:
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.id == space_id))
            if row is None:
                return None
            return self._space_summary(row)

    def get_space_by_thread(self, *, thread_id: str) -> SpaceSummary | None:
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.discord_thread_id == thread_id))
            if row is None:
                return None
            return self._space_summary(row)

    def get_actors_for_space(self, *, space_id: str) -> ActorListResponse | None:
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.id == space_id))
            if row is None:
                return None
            participants = list(
                db.scalars(
                    select(OpsParticipantModel)
                    .where(OpsParticipantModel.thread_id == row.id)
                    .order_by(OpsParticipantModel.last_event_at.desc().nullslast(), OpsParticipantModel.actor_name),
                ),
            )
            return self._actor_response(row.id, participants)

    def get_actors_for_thread(self, *, thread_id: str) -> ActorListResponse | None:
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.discord_thread_id == thread_id))
            if row is None:
                return None
            participants = list(
                db.scalars(
                    select(OpsParticipantModel)
                    .where(OpsParticipantModel.thread_id == row.id)
                    .order_by(OpsParticipantModel.last_event_at.desc().nullslast(), OpsParticipantModel.actor_name),
                ),
            )
            return self._actor_response(row.id, participants)

    def get_events_for_space(self, *, space_id: str, limit: int = 20) -> EventListResponse | None:
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.id == space_id))
            if row is None:
                return None
            events = list(
                db.scalars(
                    select(OpsEventModel)
                    .where(OpsEventModel.thread_id == row.id)
                    .order_by(OpsEventModel.created_at.desc())
                    .limit(limit),
                ),
            )
            return self._event_response(row.id, events)

    def get_events_for_thread(self, *, thread_id: str, limit: int = 20) -> EventListResponse | None:
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.discord_thread_id == thread_id))
            if row is None:
                return None
            events = list(
                db.scalars(
                    select(OpsEventModel)
                    .where(OpsEventModel.thread_id == row.id)
                    .order_by(OpsEventModel.created_at.desc())
                    .limit(limit),
                ),
            )
            return self._event_response(row.id, events)

    def _space_summary(self, row: OpsThreadModel) -> SpaceSummary:
        return SpaceSummary(
            id=row.id,
            domain_type="ops",
            transport_kind="discord_thread",
            transport_address=row.discord_thread_id,
            title=row.title,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            actors=[],
            metadata={
                "summary": row.summary,
                "issue_count": row.issue_count,
                "note_count": row.note_count,
                "last_actor_name": row.last_actor_name,
                "last_event_kind": row.last_event_kind,
                "last_event_preview": row.last_event_preview,
            },
        )

    def _actor_response(
        self,
        space_id: str,
        participants: list[OpsParticipantModel],
    ) -> ActorListResponse:
        return ActorListResponse(
            space_id=space_id,
            domain_type="ops",
            actors=[
                ActorSummary(
                    name=participant.actor_name,
                    kind=participant.kind,
                    status="active",
                    detail=participant.last_event_preview,
                    turns=participant.event_count,
                    last_active_at=participant.last_event_at,
                )
                for participant in participants
            ],
        )

    def _event_response(
        self,
        space_id: str,
        events: list[OpsEventModel],
    ) -> EventListResponse:
        return EventListResponse(
            space_id=space_id,
            domain_type="ops",
            events=[
                EventSummary(
                    id=event.id,
                    kind=event.event_kind,
                    actor_name=event.actor_name,
                    content=event.content,
                    created_at=event.created_at,
                )
                for event in events
            ],
        )


def build_ops_kernel_binding() -> KernelBehaviorBinding:
    provider = OpsKernelProvider()
    return KernelBehaviorBinding(
        behavior_id="ops",
        space_provider=provider,
        actor_provider=provider,
        event_provider=provider,
    )
