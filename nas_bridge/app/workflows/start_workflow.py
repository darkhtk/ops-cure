from __future__ import annotations

import json
import ntpath
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import session_scope
from ..models import (
    ExecutionTargetModel,
    PowerTargetModel,
    SessionModel,
    SessionOperationModel,
)
from ..schemas import ProjectManifest, SessionSummaryResponse
from ..services.policy_service import PolicyService
from ..services.recovery_service import RecoveryService

START_REUSE_SESSION_STATUSES = {
    "requested",
    "waking_execution_plane",
    "awaiting_launcher",
    "waiting_for_workers",
    "launching",
    "restarting_workers",
    "resuming_jobs",
    "ready",
    "paused",
}


class StartWorkflow:
    def __init__(
        self,
        *,
        session_service,
        policy_service: PolicyService,
        recovery_service: RecoveryService,
        announcement_service,
    ) -> None:
        self.session_service = session_service
        self.policy_service = policy_service
        self.recovery_service = recovery_service
        self.announcement_service = announcement_service

    async def run(
        self,
        *,
        project_name: str,
        target_project_name: str | None,
        preset: str | None,
        user_id: str,
        guild_id: str,
        parent_channel_id: str,
        workdir_override: str | None = None,
    ) -> SessionSummaryResponse:
        requested_target = (target_project_name or project_name).strip()
        manifest = None
        selected_preset = preset
        resolve_error: Exception | None = None
        try:
            manifest, selected_preset = self.session_service._resolve_manifest(preset)
            self.session_service._validate_session_start(
                manifest,
                user_id=user_id,
                guild_id=guild_id,
                parent_channel_id=parent_channel_id,
            )
        except Exception as exc:  # noqa: BLE001
            resolve_error = exc

        existing_session_id = self._find_existing_session_id(
            project_name=project_name,
            target_project_name=requested_target,
            selected_preset=selected_preset,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
        )
        if existing_session_id is not None:
            if manifest is not None:
                self._ensure_session_capabilities(
                    session_id=existing_session_id,
                    manifest=manifest,
                    requested_by=user_id,
                )
            await self.recovery_service.recover_session(
                session_id=existing_session_id,
                reason="project-start-reuse",
                requested_by=user_id,
                wake_if_needed=True,
            )
            await self.announcement_service.sync_session_status(existing_session_id, force=True)
            return await self.session_service.get_session_summary(existing_session_id)
        if manifest is None:
            assert resolve_error is not None
            raise resolve_error

        resolved_target_name, resolved_workdir = await self._resolve_target_workdir(
            manifest=manifest,
            selected_preset=selected_preset,
            requested_target=requested_target,
            user_id=user_id,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            workdir_override=workdir_override,
        )

        summary = await self.session_service._create_session_with_manifest(
            project_name=project_name,
            target_project_name=resolved_target_name,
            selected_preset=selected_preset,
            manifest=manifest,
            user_id=user_id,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            workdir_override=resolved_workdir,
        )
        self._ensure_session_capabilities(
            session_id=summary.id,
            manifest=manifest,
            requested_by=user_id,
        )
        await self.announcement_service.sync_session_status(summary.id, force=True)
        await self.recovery_service.recover_session(
            session_id=summary.id,
            reason="project-start",
            requested_by=user_id,
            wake_if_needed=True,
        )
        await self.announcement_service.sync_session_status(summary.id, force=True)
        return await self.session_service.get_session_summary(summary.id)

    def _find_existing_session_id(
        self,
        *,
        project_name: str,
        target_project_name: str,
        selected_preset: str | None,
        guild_id: str,
        parent_channel_id: str,
    ) -> str | None:
        with session_scope() as db:
            statement = (
                select(SessionModel)
                .where(SessionModel.project_name == project_name)
                .where(SessionModel.target_project_name == target_project_name)
                .where(SessionModel.guild_id == guild_id)
                .where(SessionModel.parent_channel_id == parent_channel_id)
                .where(SessionModel.closed_at.is_(None))
                .order_by(SessionModel.created_at.desc())
            )
            if selected_preset is not None:
                statement = statement.where(SessionModel.preset == selected_preset)
            session_row = db.scalar(statement)
            if session_row is not None:
                return session_row.id

            fallback_statement = (
                select(SessionModel)
                .where(SessionModel.target_project_name == target_project_name)
                .where(SessionModel.guild_id == guild_id)
                .where(SessionModel.parent_channel_id == parent_channel_id)
                .where(SessionModel.closed_at.is_(None))
                .where(SessionModel.status.in_(START_REUSE_SESSION_STATUSES))
                .order_by(SessionModel.created_at.desc())
            )
            if selected_preset is not None:
                fallback_statement = fallback_statement.where(SessionModel.preset == selected_preset)
            fallback_row = db.scalar(fallback_statement)
            if fallback_row is None:
                return None
            return fallback_row.id

    async def _resolve_target_workdir(
        self,
        *,
        manifest: ProjectManifest,
        selected_preset: str,
        requested_target: str,
        user_id: str,
        guild_id: str,
        parent_channel_id: str,
        workdir_override: str | None,
    ) -> tuple[str, str | None]:
        if workdir_override:
            return requested_target, workdir_override

        if self._matches_manifest_default_target(manifest, requested_target):
            return manifest.resolved_default_target_name, manifest.default_workdir

        if not manifest.finder.roots:
            raise ValueError(
                f"Target `{requested_target}` does not match profile `{selected_preset}` and this profile "
                "does not expose any finder roots for resolution."
            )

        find_summary = await self.session_service.enqueue_project_find(
            query_text=requested_target,
            preset=selected_preset,
            user_id=user_id,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
        )
        resolved = await self.session_service.wait_for_project_find(find_id=find_summary.id)
        if resolved is None:
            raise ValueError(
                f"Target resolution for `{requested_target}` is still running on the PC launcher. "
                "Try `/project start` again in a moment."
            )
        if resolved.status != "selected" or not resolved.selected_path:
            reason = resolved.reason or "no matching project path was selected"
            raise ValueError(
                f"Opscure could not safely resolve `{requested_target}` with profile `{selected_preset}`: {reason}"
            )
        default_workdir = self._normalize_path_for_comparison(manifest.default_workdir)
        selected_path = self._normalize_path_for_comparison(resolved.selected_path)
        if (
            requested_target.casefold() != manifest.resolved_default_target_name.casefold()
            and selected_path == default_workdir
        ):
            raise ValueError(
                f"Target `{requested_target}` resolved back to the profile default workdir `{manifest.default_workdir}`. "
                "Refusing to start because the requested target and profile default still look mismatched."
            )
        return resolved.selected_name or requested_target, self._canonical_workdir_value(resolved.selected_path)

    @classmethod
    def _matches_manifest_default_target(cls, manifest: ProjectManifest, requested_target: str) -> bool:
        normalized = requested_target.casefold()
        return normalized in {
            manifest.resolved_default_target_name.casefold(),
            cls._basename_from_any_path(manifest.default_workdir).casefold(),
            cls._normalize_path_for_comparison(manifest.default_workdir).casefold(),
            manifest.profile_name.casefold(),
        }

    @staticmethod
    def _looks_like_windows_path(raw_path: str) -> bool:
        value = raw_path.strip()
        drive, _ = ntpath.splitdrive(value)
        return bool(drive) or value.startswith("\\\\")

    @classmethod
    def _normalize_path_for_comparison(cls, raw_path: str) -> str:
        value = raw_path.strip()
        if cls._looks_like_windows_path(value):
            return ntpath.normcase(ntpath.normpath(value))
        return str(Path(value).resolve())

    @classmethod
    def _canonical_workdir_value(cls, raw_path: str) -> str:
        value = raw_path.strip()
        if cls._looks_like_windows_path(value):
            return ntpath.normpath(value)
        return str(Path(value).resolve())

    @staticmethod
    def _basename_from_any_path(raw_path: str) -> str:
        value = raw_path.strip().rstrip("\\/")
        if not value:
            return ""
        return ntpath.basename(value) or Path(value).name

    def _ensure_session_capabilities(
        self,
        *,
        session_id: str,
        manifest: ProjectManifest,
        requested_by: str,
    ) -> None:
        with session_scope() as db:
            session_row = db.scalar(
                select(SessionModel)
                .options(selectinload(SessionModel.agents))
                .where(SessionModel.id == session_id),
            )
            if session_row is None:
                raise ValueError("Session not found while preparing start workflow.")

            profile_key = session_row.preset or manifest.profile_name
            power_name = f"{profile_key}:{manifest.power.target}"
            execution_name = f"{profile_key}:{manifest.execution.target}"
            power_target = db.scalar(
                select(PowerTargetModel).where(PowerTargetModel.name == power_name),
            )
            if power_target is None:
                power_target = PowerTargetModel(
                    name=power_name,
                    provider=manifest.power.provider,
                    mac_address=manifest.power.mac_address,
                    broadcast_ip=manifest.power.broadcast_ip,
                    metadata_json=json.dumps(manifest.power.metadata, ensure_ascii=False),
                )
                db.add(power_target)

            execution_target = db.scalar(
                select(ExecutionTargetModel).where(ExecutionTargetModel.name == execution_name),
            )
            if execution_target is None:
                execution_target = ExecutionTargetModel(
                    name=execution_name,
                    provider=manifest.execution.provider,
                    platform=manifest.execution.platform,
                    launcher_id_hint=manifest.execution.launcher_id_hint,
                    host_pattern=manifest.execution.host_pattern,
                    metadata_json=json.dumps(
                        {
                            **manifest.execution.metadata,
                            "auto_start_expected": manifest.execution.auto_start_expected,
                        },
                        ensure_ascii=False,
                    ),
                )
                db.add(execution_target)

            session_row.power_target_name = power_name
            session_row.execution_target_name = execution_name
            session_row.desired_status = "ready"
            session_row.policy_version = session_row.policy_version or 1
            self.policy_service.ensure_policy(
                db,
                session_id=session_row.id,
                manifest=manifest,
                updated_by=requested_by,
            )
            db.add(
                SessionOperationModel(
                    session_id=session_row.id,
                    operation_type="start",
                    status="pending",
                    requested_by=requested_by,
                    input_json=json.dumps(
                        {
                            "profile": session_row.preset or manifest.profile_name,
                            "session_title": session_row.project_name,
                            "target_project_name": session_row.target_project_name or session_row.project_name,
                            "workdir": session_row.workdir,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
