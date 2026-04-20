from __future__ import annotations

from sqlalchemy import select

from ..db import session_scope
from ..models import SessionModel
from ..schemas import PolicySetResponse, SessionPolicyResponse
from ..services.policy_service import PolicyService


class PolicyWorkflow:
    def __init__(self, *, session_service, policy_service: PolicyService, announcement_service) -> None:
        self.session_service = session_service
        self.policy_service = policy_service
        self.announcement_service = announcement_service

    async def show(self, *, session_id: str) -> SessionPolicyResponse:
        with session_scope() as db:
            session_row = db.scalar(select(SessionModel).where(SessionModel.id == session_id))
            if session_row is None:
                raise ValueError("Session not found.")
            manifest, _ = self.session_service._resolve_manifest(session_row.preset)
            response = self.policy_service.get_policy_response(
                db,
                session_id=session_id,
                manifest=manifest,
            )
            if response is None:
                raise ValueError("Policy not found.")
            return response

    async def set(
        self,
        *,
        session_id: str,
        key: str,
        value: str,
        updated_by: str,
    ) -> PolicySetResponse:
        with session_scope() as db:
            session_row = db.scalar(select(SessionModel).where(SessionModel.id == session_id))
            if session_row is None:
                raise ValueError("Session not found.")
            manifest, _ = self.session_service._resolve_manifest(session_row.preset)
            response = self.policy_service.set_policy_value(
                db,
                session_id=session_id,
                manifest=manifest,
                key=key,
                raw_value=value,
                updated_by=updated_by,
            )
            session_row.policy_version = response.version
            result = PolicySetResponse(session_id=session_id, policy=response)
        await self.announcement_service.sync_session_status(session_id, force=True)
        return result
