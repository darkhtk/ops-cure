from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..behaviors.workflow.models import SessionPolicyModel
from ..behaviors.workflow.schemas import ProjectManifest, ProjectPolicy, SessionPolicyResponse


class PolicyService:
    def build_default_policy(self, manifest: ProjectManifest) -> ProjectPolicy:
        return ProjectPolicy.model_validate(manifest.policy.model_dump())

    def ensure_policy(
        self,
        db: Session,
        *,
        session_id: str,
        manifest: ProjectManifest,
        updated_by: str,
    ) -> SessionPolicyModel:
        policy_row = db.scalar(
            select(SessionPolicyModel).where(SessionPolicyModel.session_id == session_id),
        )
        if policy_row is not None:
            return policy_row

        policy = self.build_default_policy(manifest)
        policy_row = SessionPolicyModel(
            session_id=session_id,
            source="preset",
            policy_json=json.dumps(policy.model_dump(), ensure_ascii=False),
            version=1,
            updated_by=updated_by,
        )
        db.add(policy_row)
        db.flush()
        return policy_row

    def get_policy_response(
        self,
        db: Session,
        *,
        session_id: str,
        manifest: ProjectManifest | None = None,
    ) -> SessionPolicyResponse | None:
        policy_row = db.scalar(
            select(SessionPolicyModel).where(SessionPolicyModel.session_id == session_id),
        )
        if policy_row is None:
            if manifest is None:
                return None
            policy = self.build_default_policy(manifest)
            return SessionPolicyResponse(
                **policy.model_dump(),
                source="preset",
                version=1,
                updated_by="system",
                updated_at=None,
            )

        policy = ProjectPolicy.model_validate(json.loads(policy_row.policy_json))
        return SessionPolicyResponse(
            **policy.model_dump(),
            source=policy_row.source,
            version=policy_row.version,
            updated_by=policy_row.updated_by,
            updated_at=policy_row.updated_at,
        )

    def set_policy_value(
        self,
        db: Session,
        *,
        session_id: str,
        manifest: ProjectManifest,
        key: str,
        raw_value: str,
        updated_by: str,
    ) -> SessionPolicyResponse:
        policy_row = self.ensure_policy(
            db,
            session_id=session_id,
            manifest=manifest,
            updated_by=updated_by,
        )
        policy_data = json.loads(policy_row.policy_json)
        if key not in policy_data:
            raise ValueError(f"Unknown policy key `{key}`.")
        policy_data[key] = self._coerce_value(raw_value, existing_value=policy_data[key])
        policy = ProjectPolicy.model_validate(policy_data)
        policy_row.policy_json = json.dumps(policy.model_dump(), ensure_ascii=False)
        policy_row.source = "session_override"
        policy_row.version += 1
        policy_row.updated_by = updated_by
        db.flush()
        return SessionPolicyResponse(
            **policy.model_dump(),
            source=policy_row.source,
            version=policy_row.version,
            updated_by=policy_row.updated_by,
            updated_at=policy_row.updated_at,
        )

    @staticmethod
    def _coerce_value(raw_value: str, *, existing_value: object) -> object:
        normalized = raw_value.strip()
        if isinstance(existing_value, bool):
            if normalized.lower() in {"true", "1", "yes", "on"}:
                return True
            if normalized.lower() in {"false", "0", "no", "off"}:
                return False
            raise ValueError(f"Cannot coerce `{raw_value}` to boolean.")
        if isinstance(existing_value, int) and not isinstance(existing_value, bool):
            try:
                return int(normalized)
            except ValueError as exc:
                raise ValueError(f"Cannot coerce `{raw_value}` to integer.") from exc
        return normalized
