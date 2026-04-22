from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import session_scope
from ..behaviors.workflow.models import ReviewDecisionModel, VerifyArtifactModel, VerifyRunModel
from ..behaviors.workflow.schemas import (
    ProjectManifest,
    ReviewDecisionSummary,
    VerifyArtifactInput,
    VerifyArtifactSummary,
    VerifyRunClaimResponse,
    VerifyRunSummaryResponse,
)
from ..kernel.models import SessionModel
from ..transcript_service import TranscriptService
from ..thread_manager import ThreadManager
from ..worker_registry import WorkerRegistry

TERMINAL_VERIFY_STATES = {"completed", "failed", "approved", "rejected"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VerificationService:
    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        transcript_service: TranscriptService,
        thread_manager: ThreadManager,
        announcement_service,
    ) -> None:
        self.registry = registry
        self.transcript_service = transcript_service
        self.thread_manager = thread_manager
        self.announcement_service = announcement_service

    async def enqueue_run(
        self,
        *,
        session_id: str,
        mode: str,
        requested_by: str,
    ) -> VerifyRunSummaryResponse:
        with session_scope() as db:
            session_row = self._require_session(db, session_id)
            manifest = self._load_manifest_for_session(session_row)
            verification = manifest.verification
            if not verification.enabled:
                raise ValueError(f"Verification is disabled for profile `{manifest.profile_name}`.")
            command = verification.commands.get(mode)
            if not command:
                available = ", ".join(sorted(verification.commands)) or "none"
                raise ValueError(
                    f"Verification mode `{mode}` is not configured for profile `{manifest.profile_name}`. "
                    f"Available modes: {available}.",
                )

            run = VerifyRunModel(
                session_id=session_row.id,
                requested_by=requested_by,
                profile_name=manifest.profile_name,
                mode=mode,
                provider=verification.provider,
                workdir=session_row.workdir,
                artifact_dir=str(self._artifact_dir_for_run(session_row, manifest)),
                timeout_seconds=verification.run_timeout_seconds,
                command_json=json.dumps(command, ensure_ascii=False),
                status="pending",
                review_required=verification.review.require_operator_approval,
            )
            db.add(run)
            db.flush()
            run.artifact_dir = str(self._artifact_dir_for_run(session_row, manifest, run.id))

            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor="verification",
                content=(
                    f"Queued verification run `{run.id}` in mode `{mode}` for profile "
                    f"`{manifest.profile_name}`."
                ),
            )
            db.flush()
            return self._to_summary(run, session_row)

    async def claim_runs(self, *, launcher_id: str, capacity: int) -> list[VerifyRunClaimResponse]:
        profile_map = self.registry.get_projects_for_launcher(launcher_id)
        if not profile_map:
            return []

        with session_scope() as db:
            pending_runs = list(
                db.scalars(
                    select(VerifyRunModel)
                    .options(selectinload(VerifyRunModel.session))
                    .where(VerifyRunModel.status == "pending")
                    .where(VerifyRunModel.profile_name.in_(tuple(profile_map.keys())))
                    .order_by(VerifyRunModel.created_at.asc())
                    .limit(capacity),
                ),
            )
            claimed: list[VerifyRunClaimResponse] = []
            for run in pending_runs:
                run.status = "claimed"
                run.launcher_id = launcher_id
                run.claimed_at = run.claimed_at or utcnow()
                session_row = run.session
                if session_row is None:
                    continue
                claimed.append(
                    VerifyRunClaimResponse(
                        id=run.id,
                        session_id=run.session_id,
                        project_name=session_row.project_name,
                        target_project_name=session_row.target_project_name,
                        profile_name=run.profile_name,
                        mode=run.mode,
                        provider=run.provider,
                        workdir=run.workdir,
                        artifact_dir=run.artifact_dir,
                        timeout_seconds=run.timeout_seconds,
                        command=json.loads(run.command_json),
                        created_at=run.created_at,
                    ),
                )
            return claimed

    async def complete_run(
        self,
        *,
        run_id: str,
        launcher_id: str,
        status: str,
        summary_text: str | None,
        error_text: str | None,
        artifacts: list[VerifyArtifactInput],
    ) -> VerifyRunSummaryResponse:
        thread_id = ""
        thread_message = ""
        session_id = ""
        with session_scope() as db:
            run = self._require_run(db, run_id)
            session_row = self._require_session(db, run.session_id)
            session_id = session_row.id
            if run.launcher_id and run.launcher_id != launcher_id:
                raise PermissionError(
                    f"Verification run `{run_id}` is assigned to launcher `{run.launcher_id}`, not `{launcher_id}`.",
                )
            if run.status in TERMINAL_VERIFY_STATES | {"review_pending"}:
                return self._to_summary(run, session_row)

            run.launcher_id = launcher_id
            run.summary_text = summary_text
            run.error_text = error_text
            run.completed_at = run.completed_at or utcnow()
            run.status = self._resolve_completion_status(run=run, requested_status=status)

            for artifact in artifacts:
                run.artifacts.append(
                    VerifyArtifactModel(
                        artifact_type=artifact.artifact_type,
                        label=artifact.label,
                        path=artifact.path,
                    ),
                )

            self.transcript_service.add_entry(
                db,
                session_id=session_row.id,
                direction="system",
                actor="verification",
                content=self._build_completion_transcript(run, artifacts),
            )
            thread_id = session_row.discord_thread_id
            thread_message = self._build_thread_message(run, artifacts)
            db.flush()
            summary = self._to_summary(self._require_run(db, run.id), session_row)

        if thread_message:
            await self.thread_manager.post_message(thread_id, thread_message)
        await self.announcement_service.sync_session_status(session_id)
        return summary

    async def latest_run(self, *, session_id: str) -> VerifyRunSummaryResponse | None:
        with session_scope() as db:
            session_row = self._require_session(db, session_id)
            run = self._latest_run(db, session_id=session_id)
            if run is None:
                return None
            return self._to_summary(run, session_row)

    async def review_latest(
        self,
        *,
        session_id: str,
        decision: str,
        reviewer: str,
        note: str | None,
    ) -> VerifyRunSummaryResponse:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Verification review decision must be `approved` or `rejected`.")

        thread_id = ""
        thread_message = ""
        with session_scope() as db:
            session_row = self._require_session(db, session_id)
            run = self._latest_run(db, session_id=session_id, prefer_review_pending=True)
            if run is None:
                raise ValueError("There is no verification run to review in this session.")

            run.review_decisions.append(
                ReviewDecisionModel(
                    decision=decision,
                    reviewer=reviewer,
                    note=note,
                ),
            )
            run.status = decision
            run.reviewed_at = run.reviewed_at or utcnow()
            self.transcript_service.add_entry(
                db,
                session_id=session_id,
                direction="system",
                actor="verification-review",
                content=(
                    f"Verification run `{run.id}` was `{decision}` by `{reviewer}`."
                    f"{f' Note: {note}' if note else ''}"
                ),
            )
            thread_id = session_row.discord_thread_id
            thread_message = (
                f"**verification {decision}**\n"
                f"Run `{run.id}` (`{run.mode}` on `{run.profile_name}`)"
                f"{f' - {note}' if note else ''}"
            )
            db.flush()
            summary = self._to_summary(self._require_run(db, run.id), session_row)

        if thread_message:
            await self.thread_manager.post_message(thread_id, thread_message)
        await self.announcement_service.sync_session_status(session_id)
        return summary

    def _load_manifest_for_session(self, session_row: SessionModel) -> ProjectManifest:
        profile_name = session_row.preset or session_row.project_name
        manifest = self.registry.get_project(profile_name)
        if manifest is None:
            raise ValueError(
                f"Profile `{profile_name}` is not currently registered by any launcher, "
                "so verification cannot be resolved.",
            )
        return manifest

    @staticmethod
    def _artifact_dir_for_run(session_row: SessionModel, manifest: ProjectManifest, run_id: str | None = None) -> Path:
        run_slug = run_id or "pending"
        return Path(session_row.workdir).resolve() / manifest.verification.artifact_dir / session_row.id / run_slug

    @staticmethod
    def _resolve_completion_status(*, run: VerifyRunModel, requested_status: str) -> str:
        normalized = requested_status.strip().lower()
        if normalized == "completed":
            return "review_pending" if run.review_required else "completed"
        return "failed"

    @staticmethod
    def _build_completion_transcript(run: VerifyRunModel, artifacts: list[VerifyArtifactInput]) -> str:
        artifact_preview = ", ".join(
            f"{artifact.artifact_type}:{artifact.label}"
            for artifact in artifacts[:4]
        ) or "no artifacts"
        return (
            f"Verification run `{run.id}` finished with status `{run.status}` "
            f"in mode `{run.mode}`. Artifacts: {artifact_preview}."
        )

    @staticmethod
    def _build_thread_message(run: VerifyRunModel, artifacts: list[VerifyArtifactInput]) -> str:
        headline = f"**verification {run.status}**\nRun `{run.mode}` on profile `{run.profile_name}`."
        summary = (run.summary_text or run.error_text or "").strip()
        if summary:
            headline += f"\n{summary}"
        if artifacts:
            lines = [
                f"- `{artifact.label}` [{artifact.artifact_type}] `{artifact.path}`"
                for artifact in artifacts[:5]
            ]
            headline += "\nArtifacts:\n" + "\n".join(lines)
        if run.status == "review_pending":
            headline += "\nUse `/verify approve` or `/verify reject` after inspecting the artifacts."
        return headline

    def _to_summary(self, run: VerifyRunModel, session_row: SessionModel) -> VerifyRunSummaryResponse:
        latest_review = None
        if run.review_decisions:
            review = max(run.review_decisions, key=lambda item: item.created_at)
            latest_review = ReviewDecisionSummary(
                id=review.id,
                decision=review.decision,
                reviewer=review.reviewer,
                note=review.note,
                created_at=review.created_at,
            )
        artifacts = [
            VerifyArtifactSummary(
                id=artifact.id,
                artifact_type=artifact.artifact_type,
                label=artifact.label,
                path=artifact.path,
                created_at=artifact.created_at,
            )
            for artifact in sorted(run.artifacts, key=lambda item: item.created_at)
        ]
        return VerifyRunSummaryResponse(
            id=run.id,
            session_id=run.session_id,
            project_name=session_row.project_name,
            target_project_name=session_row.target_project_name,
            profile_name=run.profile_name,
            mode=run.mode,
            provider=run.provider,
            status=run.status,
            requested_by=run.requested_by,
            launcher_id=run.launcher_id,
            review_required=run.review_required,
            summary_text=run.summary_text,
            error_text=run.error_text,
            artifact_dir=run.artifact_dir,
            created_at=run.created_at,
            claimed_at=run.claimed_at,
            completed_at=run.completed_at,
            reviewed_at=run.reviewed_at,
            artifacts=artifacts,
            latest_review=latest_review,
        )

    @staticmethod
    def _require_session(db: Session, session_id: str) -> SessionModel:
        row = db.scalar(select(SessionModel).where(SessionModel.id == session_id))
        if row is None:
            raise ValueError(f"Session `{session_id}` does not exist.")
        return row

    @staticmethod
    def _require_run(db: Session, run_id: str) -> VerifyRunModel:
        row = db.scalar(
            select(VerifyRunModel)
            .options(
                selectinload(VerifyRunModel.artifacts),
                selectinload(VerifyRunModel.review_decisions),
            )
            .where(VerifyRunModel.id == run_id),
        )
        if row is None:
            raise ValueError(f"Verification run `{run_id}` does not exist.")
        return row

    @staticmethod
    def _latest_run(
        db: Session,
        *,
        session_id: str,
        prefer_review_pending: bool = False,
    ) -> VerifyRunModel | None:
        if prefer_review_pending:
            review_pending = db.scalar(
                select(VerifyRunModel)
                .options(
                    selectinload(VerifyRunModel.artifacts),
                    selectinload(VerifyRunModel.review_decisions),
                )
                .where(VerifyRunModel.session_id == session_id)
                .where(VerifyRunModel.status == "review_pending")
                .order_by(VerifyRunModel.created_at.desc()),
            )
            if review_pending is not None:
                return review_pending
        return db.scalar(
            select(VerifyRunModel)
            .options(
                selectinload(VerifyRunModel.artifacts),
                selectinload(VerifyRunModel.review_decisions),
            )
            .where(VerifyRunModel.session_id == session_id)
            .order_by(VerifyRunModel.created_at.desc()),
        )
