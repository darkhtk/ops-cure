from __future__ import annotations

import json

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


class StartWorkflow:
    def __init__(
        self,
        *,
        session_service,
        policy_service: PolicyService,
        recovery_service: RecoveryService,
    ) -> None:
        self.session_service = session_service
        self.policy_service = policy_service
        self.recovery_service = recovery_service

    async def run(
        self,
        *,
        project_name: str,
        preset: str | None,
        user_id: str,
        guild_id: str,
        parent_channel_id: str,
        workdir_override: str | None = None,
    ) -> SessionSummaryResponse:
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
            return await self.session_service.get_session_summary(existing_session_id)
        if manifest is None:
            assert resolve_error is not None
            raise resolve_error

        summary = await self.session_service._create_session_with_manifest(
            project_name=project_name,
            selected_preset=selected_preset,
            manifest=manifest,
            user_id=user_id,
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            workdir_override=workdir_override,
        )
        self._ensure_session_capabilities(
            session_id=summary.id,
            manifest=manifest,
            requested_by=user_id,
        )
        await self.recovery_service.recover_session(
            session_id=summary.id,
            reason="project-start",
            requested_by=user_id,
            wake_if_needed=True,
        )
        return await self.session_service.get_session_summary(summary.id)

    def _find_existing_session_id(
        self,
        *,
        project_name: str,
        selected_preset: str | None,
        guild_id: str,
        parent_channel_id: str,
    ) -> str | None:
        with session_scope() as db:
            statement = (
                select(SessionModel)
                .where(SessionModel.project_name == project_name)
                .where(SessionModel.guild_id == guild_id)
                .where(SessionModel.parent_channel_id == parent_channel_id)
                .where(SessionModel.closed_at.is_(None))
                .order_by(SessionModel.created_at.desc())
            )
            if selected_preset is not None:
                statement = statement.where(SessionModel.preset == selected_preset)
            session_row = db.scalar(statement)
            if session_row is None:
                return None
            return session_row.id

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

            power_name = f"{manifest.project_name}:{manifest.power.target}"
            execution_name = f"{manifest.project_name}:{manifest.execution.target}"
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
                            "preset": manifest.project_name,
                            "workdir": session_row.workdir,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
