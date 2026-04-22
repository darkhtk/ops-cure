"""Ops behavior services."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ...kernel.events import EventEnvelope, EventSummary, encode_event_cursor
from ...kernel.storage import session_scope
from ...transcript_service import sanitize_text
from ...transports.discord.threads import ThreadManager
from .models import OpsEventModel, OpsParticipantModel, OpsThreadModel
from .schemas import OpsThreadCreateResponse, OpsThreadSummary


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview_text(content: str, limit: int = 120) -> str:
    compact = " ".join(content.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _classify_event(content: str) -> tuple[str, str | None]:
    normalized = content.strip()
    lowered = normalized.lower()
    if lowered.startswith("issue:") or lowered.startswith("!issue "):
        return ("issue", "needs_attention")
    if lowered.startswith("resolve:") or lowered.startswith("!resolve "):
        return ("resolve", "monitoring")
    if lowered.startswith("status:") or lowered.startswith("!status "):
        payload = normalized.split(":", 1)[1].strip() if ":" in normalized else normalized[8:].strip()
        if payload:
            return ("status", payload)
    return ("note", None)


class OpsBehaviorService:
    def __init__(self, *, thread_manager: ThreadManager, subscription_broker=None) -> None:
        self.thread_manager = thread_manager
        self.subscription_broker = subscription_broker

    async def create_ops_thread(
        self,
        *,
        guild_id: str,
        parent_channel_id: str,
        title: str,
        summary: str | None,
        created_by: str,
        auto_archive_duration: int = 1440,
    ) -> OpsThreadCreateResponse:
        thread_id = await self.thread_manager.create_thread(
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            title=title,
            starter_text=f"Ops room opened for `{title}`.",
            auto_archive_duration=auto_archive_duration,
        )
        with session_scope() as db:
            row = OpsThreadModel(
                guild_id=guild_id,
                parent_channel_id=parent_channel_id,
                discord_thread_id=thread_id,
                title=title,
                summary=summary,
                created_by=created_by,
            )
            db.add(row)
            db.flush()
            response = OpsThreadCreateResponse(
                id=row.id,
                discord_thread_id=row.discord_thread_id,
                title=row.title,
            )
        await self.thread_manager.post_message(
            thread_id,
            (
                "이 스레드는 간단한 ops/incident room이다.\n"
                f"- 요약: `{summary or 'none'}`\n"
                "- `issue:`로 이슈를 올리고 `resolve:`로 정리할 수 있다.\n"
                "- `status:`로 현재 상태를 직접 지정할 수 있다.\n"
                "- `state` 또는 `/ops state`로 현재 room 상태를 본다."
            ),
        )
        return response

    def get_ops_thread(self, *, thread_id: str) -> OpsThreadSummary | None:
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.discord_thread_id == thread_id))
            if row is None:
                return None
            return self._summary_from_row(row)

    def record_message(self, *, thread_id: str, actor_name: str, content: str) -> OpsThreadSummary | None:
        envelope: EventEnvelope | None = None
        with session_scope() as db:
            row = db.scalar(select(OpsThreadModel).where(OpsThreadModel.discord_thread_id == thread_id))
            if row is None:
                return None

            clean_content = sanitize_text(content)
            preview = _preview_text(clean_content)
            now = utcnow()
            event_kind, implied_status = _classify_event(clean_content)

            if event_kind == "issue":
                row.issue_count += 1
                row.status = implied_status or row.status
            elif event_kind == "note":
                row.note_count += 1
            elif event_kind in {"resolve", "status"} and implied_status:
                row.status = implied_status

            row.last_actor_name = actor_name
            row.last_event_kind = event_kind
            row.last_event_preview = preview
            row.last_event_at = now

            event = OpsEventModel(
                thread_id=row.id,
                actor_name=actor_name,
                event_kind=event_kind,
                content=clean_content,
            )
            db.add(event)
            db.flush()
            envelope = EventEnvelope(
                cursor=encode_event_cursor(created_at=event.created_at, event_id=event.id),
                space_id=row.id,
                event=EventSummary(
                    id=event.id,
                    kind=event.event_kind,
                    actor_name=event.actor_name,
                    content=event.content,
                    created_at=event.created_at,
                ),
            )

            participant = db.scalar(
                select(OpsParticipantModel)
                .where(OpsParticipantModel.thread_id == row.id)
                .where(OpsParticipantModel.actor_name == actor_name),
            )
            if participant is None:
                participant = OpsParticipantModel(
                    thread_id=row.id,
                    actor_name=actor_name,
                )
                db.add(participant)
            participant.event_count = (participant.event_count or 0) + 1
            participant.last_event_preview = preview
            participant.last_event_at = now

            summary = self._summary_from_row(row)
        if envelope is not None and self.subscription_broker is not None:
            self.subscription_broker.publish(space_id=envelope.space_id, item=envelope)
        return summary

    def _summary_from_row(self, row: OpsThreadModel) -> OpsThreadSummary:
        return OpsThreadSummary(
            id=row.id,
            discord_thread_id=row.discord_thread_id,
            title=row.title,
            summary=row.summary,
            status=row.status,
            issue_count=row.issue_count,
            note_count=row.note_count,
            last_actor_name=row.last_actor_name,
            last_event_kind=row.last_event_kind,
            last_event_preview=row.last_event_preview,
            last_event_at=row.last_event_at,
            created_at=row.created_at,
        )
