"""DigestService — summary card on close + daily rollup compose."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ...kernel.v2 import V2Repository
from ...kernel.v2.models import OperationV2Model

ARTIFACT_KIND_SUMMARY = "summary"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _isoformat(dt: datetime | None) -> str | None:
    aware = _ensure_aware(dt)
    return aware.isoformat() if aware else None


class DigestService:
    """Adds a summary OperationArtifact on close and composes per-space
    daily rollups. Holds no state; safe to instantiate per-request or
    once at startup."""

    def __init__(self, repo: V2Repository | None = None) -> None:
        self._repo = repo or V2Repository()

    # ------ close-time card ----------------------------------------------

    def record_close(
        self,
        db: Session,
        *,
        v2_operation_id: str | None,
        v2_close_event_id: str | None,
    ) -> str | None:
        """Build the summary card and attach it as an inline artifact.
        Returns the artifact id, or None if either argument is missing.

        Runs inside the close transaction; if the outer commit fails,
        the artifact is rolled back with everything else."""
        if not v2_operation_id or not v2_close_event_id:
            return None
        op = self._repo.get_operation(db, v2_operation_id)
        if op is None:
            return None
        # Only summarize ops that are actually closed (defensive: the
        # caller should already have closed before calling us).
        if op.state != "closed":
            return None

        summary = self._build_summary(db, op)
        summary_json = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        content_bytes = summary_json.encode("utf-8")
        b64 = base64.b64encode(content_bytes).decode("ascii")
        uri = f"data:application/json;base64,{b64}"
        sha = hashlib.sha256(content_bytes).hexdigest()

        artifact = self._repo.insert_artifact(
            db,
            operation_id=v2_operation_id,
            event_id=v2_close_event_id,
            kind=ARTIFACT_KIND_SUMMARY,
            uri=uri,
            sha256=sha,
            mime="application/json",
            size_bytes=len(content_bytes),
            label=f"digest:{op.kind}:{op.resolution or 'closed'}",
            metadata=summary,
        )
        return artifact.id

    def _build_summary(
        self,
        db: Session,
        op: OperationV2Model,
    ) -> dict[str, Any]:
        """Compose the summary dict. Pure read, no side effects.

        Categorizes events into:
          - opening_question: first speech.question (if any)
          - speech_count: total chat.speech.* rows
          - evidence_count: chat.task.evidence rows
          - whisper_count: events with non-null private_to_actor_ids
          - lifecycle_count: chat.conversation.* + chat.task.* lifecycle
            (claimed, approval_*, completed, etc.) NOT speech / evidence
          - last_addressed_speech: last speech with addressed_to set
            (the call-to-action right before close)
        """
        events = self._repo.list_events(db, operation_id=op.id, limit=1000)
        participants = self._repo.list_participants(db, operation_id=op.id)
        artifacts = self._repo.list_artifacts_for_operation(db, operation_id=op.id)

        speech_count = 0
        evidence_count = 0
        whisper_count = 0
        lifecycle_count = 0
        opening_question: str | None = None
        last_addressed_speech: dict[str, Any] | None = None

        for ev in events:
            kind = ev.kind
            if kind.startswith("chat.speech."):
                speech_count += 1
                if kind == "chat.speech.question" and opening_question is None:
                    opening_question = (
                        self._repo.event_payload(ev).get("text", "")[:200]
                    )
                addressed = self._repo.event_addressed_to(ev)
                if addressed:
                    last_addressed_speech = {
                        "seq": ev.seq,
                        "kind": kind,
                        "actor_id": ev.actor_id,
                        "addressed_to_actor_ids": addressed,
                        "text_preview": (
                            self._repo.event_payload(ev).get("text", "")[:200]
                        ),
                    }
            elif kind == "chat.task.evidence":
                evidence_count += 1
            elif kind.startswith("chat.conversation.") or kind.startswith("chat.task."):
                lifecycle_count += 1
            if self._repo.event_private_to(ev) is not None:
                whisper_count += 1

        opened_at = _ensure_aware(op.created_at)
        closed_at = _ensure_aware(op.closed_at)
        duration_seconds: int | None = None
        if opened_at and closed_at:
            duration_seconds = int((closed_at - opened_at).total_seconds())

        return {
            "operation_id": op.id,
            "space_id": op.space_id,
            "kind": op.kind,
            "title": op.title,
            "intent": op.intent,
            "resolution": op.resolution,
            "resolution_summary": op.resolution_summary,
            "closed_by_actor_id": op.closed_by_actor_id,
            "opened_at": _isoformat(op.created_at),
            "closed_at": _isoformat(op.closed_at),
            "duration_seconds": duration_seconds,
            "participants": [
                {"actor_id": p.actor_id, "role": p.role}
                for p in participants
            ],
            "totals": {
                "events": len(events),
                "speech": speech_count,
                "evidence": evidence_count,
                "lifecycle": lifecycle_count,
                "whispers": whisper_count,
                # Exclude the about-to-be-inserted summary artifact from
                # the count -- this snapshot reflects everything BEFORE
                # the artifact lands.
                "artifacts": len(artifacts),
            },
            "opening_question": opening_question,
            "last_addressed_speech": last_addressed_speech,
        }

    # ------ daily rollup -------------------------------------------------

    def compose_space_rollup(
        self,
        db: Session,
        *,
        space_id: str,
        since: datetime,
        until: datetime,
    ) -> dict[str, Any]:
        """Aggregate every op in ``space_id`` whose closed_at is in
        [since, until). Returns a structured dict the caller can render
        to markdown / discord embed / wherever.

        Caller responsibility: figure out the time window. Typical use:
        cron at 00:05 sets since = today_start - 1d, until = today_start.
        """
        from sqlalchemy import select
        # Get ops closed in window for this space.
        stmt = (
            select(OperationV2Model)
            .where(OperationV2Model.space_id == space_id)
            .where(OperationV2Model.state == "closed")
            .where(OperationV2Model.closed_at >= since)
            .where(OperationV2Model.closed_at < until)
            .order_by(OperationV2Model.closed_at.asc())
        )
        ops = list(db.scalars(stmt))

        items: list[dict[str, Any]] = []
        kind_counts: dict[str, int] = {}
        resolution_counts: dict[str, int] = {}
        for op in ops:
            kind_counts[op.kind] = kind_counts.get(op.kind, 0) + 1
            res = op.resolution or "<unresolved>"
            resolution_counts[res] = resolution_counts.get(res, 0) + 1
            opened = _ensure_aware(op.created_at)
            closed = _ensure_aware(op.closed_at)
            duration = int((closed - opened).total_seconds()) if opened and closed else None
            items.append({
                "operation_id": op.id,
                "kind": op.kind,
                "title": op.title,
                "resolution": op.resolution,
                "duration_seconds": duration,
                "closed_at": _isoformat(op.closed_at),
            })

        return {
            "space_id": space_id,
            "since": _isoformat(since),
            "until": _isoformat(until),
            "total_closed": len(ops),
            "by_kind": kind_counts,
            "by_resolution": resolution_counts,
            "items": items,
        }

    def render_rollup_markdown(self, rollup: dict[str, Any]) -> str:
        """Friendly markdown rendering of compose_space_rollup output.
        Suitable for posting as a system speech in a chat thread."""
        lines: list[str] = []
        lines.append(f"### Daily digest -- {rollup['since']} to {rollup['until']}")
        lines.append(f"closed: **{rollup['total_closed']}** operation(s)")
        if rollup["by_kind"]:
            tally = ", ".join(
                f"{k}={v}" for k, v in sorted(rollup["by_kind"].items())
            )
            lines.append(f"by kind: {tally}")
        if rollup["by_resolution"]:
            tally = ", ".join(
                f"{r}={v}" for r, v in sorted(rollup["by_resolution"].items())
            )
            lines.append(f"by resolution: {tally}")
        if rollup["items"]:
            lines.append("")
            for item in rollup["items"]:
                duration = (
                    f"{item['duration_seconds']}s" if item.get("duration_seconds") is not None
                    else "?"
                )
                lines.append(
                    f"- [{item['kind']}] {item['title']} "
                    f"-> {item['resolution']} ({duration})"
                )
        return "\n".join(lines)
