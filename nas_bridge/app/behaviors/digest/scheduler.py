"""DigestSchedulerLoop -- periodic rollup posting in lifespan.

Every ``interval_seconds`` (default 24h), iterates spaces that have
ops closed since the last fire, composes a rollup per space, and
posts it as a system speech in that space's general conversation.

Idempotent: re-firing within the same window doesn't double-post; the
loop tracks the last successful run and uses that as ``since``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ...behaviors.chat.conversation_schemas import SpeechActSubmitRequest
from ...behaviors.chat.models import ChatThreadModel, ChatConversationModel
from ...kernel.storage import session_scope
from ...kernel.v2 import V2Repository
from ...kernel.v2.models import OperationV2Model

from .service import DigestService

logger = logging.getLogger("opscure.digest.scheduler")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DigestSchedulerLoop:
    def __init__(
        self,
        *,
        chat_service,
        digest_service: DigestService | None = None,
        interval_seconds: int = 24 * 3600,
        system_actor_handle: str = "@digest-bot",
    ) -> None:
        self._chat = chat_service
        self._digest = digest_service or DigestService()
        self._interval = max(60, int(interval_seconds))
        self._system_actor = system_actor_handle.lstrip("@")
        self._stopping = False
        self._last_fire: datetime | None = None

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                self._fire_once()
            except Exception:  # noqa: BLE001
                logger.exception("digest scheduler tick failed; continuing")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    def _fire_once(self) -> int:
        """Compose + post rollups for every space with closed ops since
        last fire. Returns number of spaces posted to."""
        now = _utcnow()
        since = self._last_fire or (now - timedelta(seconds=self._interval))
        until = now
        repo = V2Repository()
        posted = 0
        with session_scope() as db:
            spaces = self._spaces_with_closed_ops(db, since=since, until=until)
        for space_id in spaces:
            with session_scope() as db:
                rollup = self._digest.compose_space_rollup(
                    db, space_id=space_id, since=since, until=until,
                )
            if rollup["total_closed"] == 0:
                continue
            md = self._digest.render_rollup_markdown(rollup)
            discord_thread_id = self._space_to_discord_thread_id(space_id)
            if discord_thread_id is None:
                logger.warning(
                    "digest: space=%s has no resolvable discord thread; skipping",
                    space_id,
                )
                continue
            general_id = self._general_conversation_id(discord_thread_id)
            if general_id is None:
                logger.warning(
                    "digest: thread=%s has no general conversation; skipping",
                    discord_thread_id,
                )
                continue
            try:
                self._chat.submit_speech(
                    conversation_id=general_id,
                    request=SpeechActSubmitRequest(
                        actor_name=self._system_actor,
                        kind="summarize",
                        content=md,
                    ),
                )
                posted += 1
            except Exception:  # noqa: BLE001
                logger.exception("digest: posting to space=%s failed", space_id)
        self._last_fire = now
        return posted

    def _spaces_with_closed_ops(
        self,
        db,
        *,
        since: datetime,
        until: datetime,
    ) -> list[str]:
        rows = db.execute(
            select(OperationV2Model.space_id)
            .where(OperationV2Model.state == "closed")
            .where(OperationV2Model.closed_at >= since)
            .where(OperationV2Model.closed_at < until)
            .distinct()
        ).all()
        return [r[0] for r in rows]

    @staticmethod
    def _space_to_discord_thread_id(space_id: str) -> str | None:
        """v2 space_id is 'chat:<chat_threads.id>' (a UUID). The digest
        post needs the discord_thread_id (string snowflake) to find the
        general conversation. Look up chat_threads."""
        if not space_id.startswith("chat:"):
            return None
        chat_thread_id = space_id[len("chat:"):]
        with session_scope() as db:
            row = db.scalar(
                select(ChatThreadModel)
                .where(ChatThreadModel.id == chat_thread_id)
                .limit(1),
            )
            return row.discord_thread_id if row else None

    @staticmethod
    def _general_conversation_id(discord_thread_id: str) -> str | None:
        with session_scope() as db:
            thread = db.scalar(
                select(ChatThreadModel)
                .where(ChatThreadModel.discord_thread_id == discord_thread_id)
                .limit(1)
            )
            if thread is None:
                return None
            general = db.scalar(
                select(ChatConversationModel)
                .where(ChatConversationModel.thread_id == thread.id)
                .where(ChatConversationModel.is_general.is_(True))
                .limit(1),
            )
            return general.id if general else None
