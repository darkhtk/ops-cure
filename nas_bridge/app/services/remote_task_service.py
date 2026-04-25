from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import case, select
from sqlalchemy.orm import selectinload

from ..db import session_scope
from ..kernel.approvals import KernelApprovalService
from ..kernel.presence import (
    ActorSessionUpsertRequest,
    PresenceService,
    ResourceLeaseClaimRequest,
    ResourceLeaseHeartbeatRequest,
    ResourceLeaseReleaseRequest,
    ResourceLeaseSummary,
)
from ..models import (
    RemoteTaskAssignmentModel,
    RemoteTaskApprovalModel,
    RemoteTaskEvidenceModel,
    RemoteTaskHeartbeatModel,
    RemoteTaskNoteModel,
    RemoteTaskModel,
)
from ..schemas import (
    RemoteTaskAssignmentSummary,
    RemoteTaskApprovalRequest,
    RemoteTaskApprovalResolveRequest,
    RemoteTaskApprovalSummary,
    RemoteTaskClaimRequest,
    RemoteTaskClaimNextRequest,
    RemoteTaskCompleteRequest,
    RemoteTaskCreateRequest,
    RemoteTaskEvidenceRequest,
    RemoteTaskEvidenceSummary,
    RemoteTaskFailRequest,
    RemoteTaskHeartbeatRequest,
    RemoteTaskHeartbeatSummary,
    RemoteTaskInterruptRequest,
    RemoteTaskNoteRequest,
    RemoteTaskNoteSummary,
    RemoteTaskSummaryResponse,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


WORK_EVIDENCE_KINDS = {
    "command_execution",
    "file_read",
    "file_write",
    "test_result",
    "runtime_turn_started",
    "runtime_turn_completed",
}


REMOTE_TASK_RESOURCE_KIND = "remote_task"
MACHINE_SCOPE_KIND = "machine"
REMOTE_TASK_APPROVAL_KIND = "remote_task.approval"
REMOTE_TASK_SPACE_PREFIX = "remote_task:"


def remote_task_space_id(task_id: str) -> str:
    return f"{REMOTE_TASK_SPACE_PREFIX}{task_id}"


class RemoteTaskService:
    def __init__(
        self,
        *,
        presence_service: PresenceService | None = None,
        kernel_approval_service: KernelApprovalService | None = None,
    ) -> None:
        self.presence_service = presence_service or PresenceService()
        self.kernel_approval_service = kernel_approval_service or KernelApprovalService()

    def create_task(self, payload: RemoteTaskCreateRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = RemoteTaskModel(
                machine_id=payload.machine_id,
                thread_id=payload.thread_id,
                origin_surface=payload.origin_surface,
                origin_message_id=payload.origin_message_id,
                objective=payload.objective,
                success_criteria_json=json.dumps(payload.success_criteria, ensure_ascii=False),
                priority=payload.priority,
                created_by=payload.created_by,
            )
            db.add(row)
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def list_tasks(
        self,
        *,
        machine_id: str | None = None,
        thread_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[RemoteTaskSummaryResponse]:
        with session_scope() as db:
            query = (
                select(RemoteTaskModel)
                .options(
                    selectinload(RemoteTaskModel.assignments),
                    selectinload(RemoteTaskModel.heartbeats),
                    selectinload(RemoteTaskModel.evidence_items),
                    selectinload(RemoteTaskModel.approvals),
                    selectinload(RemoteTaskModel.notes),
                )
                .order_by(RemoteTaskModel.updated_at.desc(), RemoteTaskModel.created_at.desc())
                .limit(max(1, min(limit, 200)))
            )
            if machine_id:
                query = query.where(RemoteTaskModel.machine_id == machine_id)
            if thread_id:
                query = query.where(RemoteTaskModel.thread_id == thread_id)
            if statuses:
                query = query.where(RemoteTaskModel.status.in_(tuple(statuses)))
            rows = list(db.scalars(query))
            return [self._to_summary(row) for row in rows]

    def get_task(self, task_id: str) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            return self._to_summary(row)

    def claim_task(self, task_id: str, payload: RemoteTaskClaimRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            self._claim_task_in_session(db=db, row=row, actor_id=payload.actor_id, lease_seconds=payload.lease_seconds)
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def claim_next_task(
        self,
        *,
        machine_id: str,
        payload: RemoteTaskClaimNextRequest,
    ) -> RemoteTaskSummaryResponse | None:
        with session_scope() as db:
            query = (
                select(RemoteTaskModel)
                .options(
                    selectinload(RemoteTaskModel.assignments),
                    selectinload(RemoteTaskModel.heartbeats),
                    selectinload(RemoteTaskModel.evidence_items),
                    selectinload(RemoteTaskModel.approvals),
                    selectinload(RemoteTaskModel.notes),
                )
                .where(
                    RemoteTaskModel.machine_id == machine_id,
                    RemoteTaskModel.status == "queued",
                )
                .order_by(
                    case(
                        (RemoteTaskModel.priority == "critical", 0),
                        (RemoteTaskModel.priority == "high", 1),
                        (RemoteTaskModel.priority == "normal", 2),
                        else_=3,
                    ),
                    RemoteTaskModel.created_at.asc(),
                )
                .limit(1)
            )
            if payload.exclude_origin_surfaces:
                query = query.where(
                    ~RemoteTaskModel.origin_surface.in_(tuple(payload.exclude_origin_surfaces)),
                )
            row = db.scalar(query)
            if row is None:
                return None
            self._claim_task_in_session(db=db, row=row, actor_id=payload.actor_id, lease_seconds=payload.lease_seconds)
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def heartbeat_task(self, task_id: str, payload: RemoteTaskHeartbeatRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            assignment = self._require_active_assignment(
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
            )
            lease = self.presence_service.get_current_lease(
                resource_kind=REMOTE_TASK_RESOURCE_KIND,
                resource_id=row.id,
                db=db,
            )
            if lease is None:
                raise ValueError(f"Remote task `{task_id}` has no active kernel lease.")
            lease = self.presence_service.heartbeat_resource_lease(
                lease_id=lease.lease_id,
                payload=ResourceLeaseHeartbeatRequest(
                    holder_actor_id=payload.actor_id,
                    lease_token=payload.lease_token,
                    lease_seconds=payload.lease_seconds,
                    status="claimed",
                ),
                db=db,
            )

            heartbeat = RemoteTaskHeartbeatModel(
                task_id=row.id,
                actor_id=payload.actor_id,
                phase=payload.phase,
                summary=payload.summary,
                commands_run_count=payload.commands_run_count,
                files_read_count=payload.files_read_count,
                files_modified_count=payload.files_modified_count,
                tests_run_count=payload.tests_run_count,
            )
            row.heartbeats.append(heartbeat)
            assignment.lease_expires_at = lease.expires_at
            row.owner_actor_id = payload.actor_id
            row.status = self._status_for_heartbeat(payload)
            row.updated_at = utcnow()
            self._upsert_machine_presence(
                db=db,
                row=row,
                actor_id=payload.actor_id,
                ttl_seconds=payload.lease_seconds,
                status=row.status,
            )
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def add_evidence(self, task_id: str, payload: RemoteTaskEvidenceRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            row.evidence_items.append(
                RemoteTaskEvidenceModel(
                    task_id=row.id,
                    actor_id=payload.actor_id,
                    kind=payload.kind,
                    summary=payload.summary,
                    payload_json=json.dumps(payload.payload, ensure_ascii=False),
                ),
            )
            if payload.kind in WORK_EVIDENCE_KINDS and row.status in {"queued", "claimed"}:
                row.status = "executing"
            row.updated_at = utcnow()
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def request_approval(self, task_id: str, payload: RemoteTaskApprovalRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            self._require_active_assignment(
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
            )
            approval_row = RemoteTaskApprovalModel(
                task_id=row.id,
                actor_id=payload.actor_id,
                reason=payload.reason,
                note=payload.note,
                status="pending",
            )
            row.approvals.append(approval_row)
            row.status = "blocked_approval"
            row.owner_actor_id = payload.actor_id
            row.updated_at = utcnow()
            db.flush()
            db.refresh(row)
            db.refresh(approval_row)
            self._mirror_approval_request(
                db,
                task_id=row.id,
                approval=approval_row,
            )
            return self._to_summary(row)

    def get_latest_approval(self, task_id: str) -> RemoteTaskApprovalSummary | None:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            approval = self._latest_approval(row)
            return self._to_approval_summary(approval) if approval is not None else None

    def resolve_approval(
        self,
        task_id: str,
        payload: RemoteTaskApprovalResolveRequest,
    ) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            approval = self._latest_approval(row)
            if approval is None or approval.status != "pending":
                raise ValueError(f"Remote task `{task_id}` has no pending approval.")
            approval.status = "resolved"
            approval.note = payload.note or approval.note
            approval.resolved_at = utcnow()
            approval.resolved_by = payload.resolved_by
            approval.resolution = payload.resolution
            row.status = "claimed" if payload.resolution == "approved" else "failed"
            row.updated_at = utcnow()
            db.flush()
            db.refresh(row)
            self._mirror_approval_resolve(
                db,
                approval_id=approval.id,
                resolution=payload.resolution,
                resolved_by=payload.resolved_by,
                note=payload.note,
            )
            return self._to_summary(row)

    def add_note(self, task_id: str, payload: RemoteTaskNoteRequest) -> RemoteTaskNoteSummary:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            note = RemoteTaskNoteModel(
                task_id=row.id,
                actor_id=payload.actor_id,
                kind=payload.kind,
                content=payload.content,
            )
            row.notes.append(note)
            row.updated_at = utcnow()
            db.flush()
            db.refresh(note)
            return self._to_note_summary(note)

    def list_notes(self, task_id: str) -> list[RemoteTaskNoteSummary]:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            notes = sorted(row.notes, key=lambda item: item.created_at)
            return [self._to_note_summary(item) for item in notes]

    def interrupt_task(self, task_id: str, payload: RemoteTaskInterruptRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            assignment = self._require_active_assignment(
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
            )
            if payload.note:
                row.notes.append(
                    RemoteTaskNoteModel(
                        task_id=row.id,
                        actor_id=payload.actor_id,
                        kind="interrupt",
                        content=payload.note,
                    ),
                )
            self._release_kernel_lease_if_present(
                db=db,
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
                status="released",
            )
            assignment.status = "interrupted"
            assignment.released_at = utcnow()
            row.status = "interrupted"
            row.owner_actor_id = payload.actor_id
            row.updated_at = utcnow()
            self._upsert_machine_presence(
                db=db,
                row=row,
                actor_id=payload.actor_id,
                ttl_seconds=60,
                status="idle",
            )
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def complete_task(self, task_id: str, payload: RemoteTaskCompleteRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            assignment = self._require_active_assignment(
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
            )
            if payload.summary:
                row.evidence_items.append(
                    RemoteTaskEvidenceModel(
                        task_id=row.id,
                        actor_id=payload.actor_id,
                        kind="result",
                        summary=payload.summary,
                        payload_json=json.dumps({"kind": "result"}, ensure_ascii=False),
                    ),
                )
            self._release_kernel_lease_if_present(
                db=db,
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
                status="released",
            )
            assignment.status = "completed"
            assignment.released_at = utcnow()
            row.status = "completed"
            row.owner_actor_id = payload.actor_id
            row.updated_at = utcnow()
            self._upsert_machine_presence(
                db=db,
                row=row,
                actor_id=payload.actor_id,
                ttl_seconds=60,
                status="idle",
            )
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def fail_task(self, task_id: str, payload: RemoteTaskFailRequest) -> RemoteTaskSummaryResponse:
        with session_scope() as db:
            row = self._require_task(db, task_id)
            assignment = self._require_active_assignment(
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
            )
            row.evidence_items.append(
                RemoteTaskEvidenceModel(
                    task_id=row.id,
                    actor_id=payload.actor_id,
                    kind="error",
                    summary=payload.error_text,
                    payload_json=json.dumps({"kind": "error"}, ensure_ascii=False),
                ),
            )
            self._release_kernel_lease_if_present(
                db=db,
                row=row,
                actor_id=payload.actor_id,
                lease_token=payload.lease_token,
                status="released",
            )
            assignment.status = "failed"
            assignment.released_at = utcnow()
            row.status = "failed"
            row.owner_actor_id = payload.actor_id
            row.updated_at = utcnow()
            self._upsert_machine_presence(
                db=db,
                row=row,
                actor_id=payload.actor_id,
                ttl_seconds=60,
                status="idle",
            )
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def settle_stale_task(
        self,
        task_id: str,
        *,
        final_status: str,
        summary: str,
        payload: dict | None = None,
    ) -> RemoteTaskSummaryResponse:
        if final_status not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"Unsupported stale task status: {final_status}")
        with session_scope() as db:
            row = self._require_task(db, task_id)
            if row.status in {"completed", "failed", "interrupted"}:
                return self._to_summary(row)

            now = utcnow()
            assignment = self._current_assignment(row)
            actor_id = (
                assignment.actor_id
                if assignment is not None
                else row.owner_actor_id
                or "bridge-service"
            )
            lease = self.presence_service.get_current_lease(
                resource_kind=REMOTE_TASK_RESOURCE_KIND,
                resource_id=row.id,
                db=db,
            )
            if (
                lease is not None
                and assignment is not None
                and lease.holder_actor_id == assignment.actor_id
                and lease.lease_token == assignment.lease_token
            ):
                self.presence_service.release_resource_lease(
                    lease_id=lease.lease_id,
                    payload=ResourceLeaseReleaseRequest(
                        holder_actor_id=assignment.actor_id,
                        lease_token=assignment.lease_token,
                        status="released",
                    ),
                    db=db,
                )

            if assignment is not None and assignment.released_at is None:
                assignment.status = final_status
                assignment.released_at = now

            evidence_kind = "result" if final_status == "completed" else "error"
            row.evidence_items.append(
                RemoteTaskEvidenceModel(
                    task_id=row.id,
                    actor_id=actor_id,
                    kind=evidence_kind,
                    summary=summary,
                    payload_json=json.dumps(
                        {
                            "kind": "stale_task_cleanup",
                            "finalStatus": final_status,
                            **(payload or {}),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            row.status = final_status
            row.owner_actor_id = actor_id
            row.updated_at = now
            db.flush()
            db.refresh(row)
            return self._to_summary(row)

    def _require_task(self, db, task_id: str) -> RemoteTaskModel:
        row = db.scalar(
            select(RemoteTaskModel)
            .options(
                selectinload(RemoteTaskModel.assignments),
                selectinload(RemoteTaskModel.heartbeats),
                selectinload(RemoteTaskModel.evidence_items),
                selectinload(RemoteTaskModel.approvals),
                selectinload(RemoteTaskModel.notes),
            )
            .where(RemoteTaskModel.id == task_id),
        )
        if row is None:
            raise ValueError(f"Remote task `{task_id}` was not found.")
        return row

    def _current_assignment(self, row: RemoteTaskModel) -> RemoteTaskAssignmentModel | None:
        claimed = [item for item in row.assignments if item.status == "claimed"]
        if not claimed:
            return None
        claimed.sort(key=lambda item: item.claimed_at, reverse=True)
        return claimed[0]

    def _latest_approval(self, row: RemoteTaskModel) -> RemoteTaskApprovalModel | None:
        if not row.approvals:
            return None
        return max(row.approvals, key=lambda item: item.requested_at)

    def _mirror_approval_request(
        self,
        db,
        *,
        task_id: str,
        approval: RemoteTaskApprovalModel,
    ) -> None:
        """Mirror a freshly-created legacy approval row into the generic
        kernel approvals primitive. The kernel record reuses the legacy
        approval id as its primary key so the two tables stay 1:1
        without an extra join column. Failures are swallowed: the legacy
        write must remain authoritative until the kernel side proves
        itself in production.
        """
        try:
            self.kernel_approval_service.request(
                db,
                space_id=remote_task_space_id(task_id),
                kind=REMOTE_TASK_APPROVAL_KIND,
                payload={
                    "task_id": task_id,
                    "actor_id": approval.actor_id,
                    "reason": approval.reason,
                },
                requested_by=approval.actor_id or "",
                approval_id=approval.id,
            )
        except Exception:  # noqa: BLE001 — mirror must not destabilize the legacy write
            pass

    def _mirror_approval_resolve(
        self,
        db,
        *,
        approval_id: str,
        resolution: str,
        resolved_by: str,
        note: str | None,
    ) -> None:
        try:
            self.kernel_approval_service.resolve(
                db,
                approval_id=approval_id,
                resolution=resolution,
                resolved_by=resolved_by or "",
                note=note,
            )
        except Exception:  # noqa: BLE001 — mirror must not destabilize the legacy write
            pass

    def _require_active_assignment(
        self,
        *,
        row: RemoteTaskModel,
        actor_id: str,
        lease_token: str,
    ) -> RemoteTaskAssignmentModel:
        assignment = self._current_assignment(row)
        if assignment is None:
            raise ValueError(f"Remote task `{row.id}` has no active assignment.")
        if assignment.actor_id != actor_id:
            raise ValueError(f"Remote task `{row.id}` is owned by `{assignment.actor_id}`, not `{actor_id}`.")
        if assignment.lease_token != lease_token:
            raise ValueError("Lease token does not match the active assignment.")
        if ensure_utc(assignment.lease_expires_at) <= utcnow():
            raise ValueError(f"Lease for remote task `{row.id}` has expired.")
        return assignment

    def _status_for_heartbeat(self, payload: RemoteTaskHeartbeatRequest) -> str:
        phase = payload.phase.strip().lower()
        has_work_signal = any(
            [
                payload.commands_run_count > 0,
                payload.files_read_count > 0,
                payload.files_modified_count > 0,
                payload.tests_run_count > 0,
            ],
        )
        if phase in {"blocked_approval", "approval", "waiting_approval"}:
            return "blocked_approval"
        if phase in {"interrupted", "interrupting"}:
            return "interrupted"
        if phase == "verifying":
            return "verifying" if has_work_signal else "claimed"
        if phase in {"executing", "working", "running"}:
            return "executing" if has_work_signal else "claimed"
        return "claimed"

    def _upsert_assignment_from_lease(
        self,
        *,
        row: RemoteTaskModel,
        assignment: RemoteTaskAssignmentModel | None,
        lease: ResourceLeaseSummary,
        now: datetime,
    ) -> RemoteTaskAssignmentModel:
        if assignment is not None and ensure_utc(assignment.lease_expires_at) <= now and assignment.released_at is None:
            assignment.status = "expired"
            assignment.released_at = now

        if (
            assignment is not None
            and assignment.actor_id == lease.holder_actor_id
            and assignment.released_at is None
            and assignment.status == "claimed"
        ):
            assignment.lease_token = lease.lease_token
            assignment.lease_expires_at = lease.expires_at
            return assignment

        assignment = RemoteTaskAssignmentModel(
            task_id=row.id,
            actor_id=lease.holder_actor_id,
            lease_token=lease.lease_token,
            lease_expires_at=lease.expires_at,
            status="claimed",
            claimed_at=lease.claimed_at,
        )
        row.assignments.append(assignment)
        return assignment

    def _upsert_machine_presence(
        self,
        *,
        db,
        row: RemoteTaskModel,
        actor_id: str,
        ttl_seconds: int,
        status: str,
    ) -> None:
        self.presence_service.upsert_actor_session(
            ActorSessionUpsertRequest(
                actor_id=actor_id,
                scope_kind=MACHINE_SCOPE_KIND,
                scope_id=row.machine_id,
                status=status,
                ttl_seconds=ttl_seconds,
            ),
            db=db,
        )

    def _release_kernel_lease_if_present(
        self,
        *,
        db,
        row: RemoteTaskModel,
        actor_id: str,
        lease_token: str,
        status: str,
    ) -> None:
        lease = self.presence_service.get_current_lease(
            resource_kind=REMOTE_TASK_RESOURCE_KIND,
            resource_id=row.id,
            db=db,
        )
        if lease is None:
            return
        if lease.holder_actor_id != actor_id or lease.lease_token != lease_token:
            raise ValueError(f"Kernel lease for remote task `{row.id}` does not match the active assignment.")
        self.presence_service.release_resource_lease(
            lease_id=lease.lease_id,
            payload=ResourceLeaseReleaseRequest(
                holder_actor_id=actor_id,
                lease_token=lease_token,
                status=status,
            ),
            db=db,
        )

    def _claim_task_in_session(
        self,
        *,
        db,
        row: RemoteTaskModel,
        actor_id: str,
        lease_seconds: int,
    ) -> None:
        assignment = self._current_assignment(row)
        now = utcnow()
        lease = self.presence_service.claim_resource_lease(
            ResourceLeaseClaimRequest(
                resource_kind=REMOTE_TASK_RESOURCE_KIND,
                resource_id=row.id,
                holder_actor_id=actor_id,
                lease_token=(
                    assignment.lease_token
                    if assignment is not None and assignment.actor_id == actor_id
                    else None
                ),
                lease_seconds=lease_seconds,
                status="claimed",
            ),
            db=db,
        )
        self._upsert_assignment_from_lease(
            row=row,
            assignment=assignment,
            lease=lease,
            now=now,
        )
        self._upsert_machine_presence(
            db=db,
            row=row,
            actor_id=actor_id,
            ttl_seconds=lease_seconds,
            status="claimed",
        )
        row.owner_actor_id = actor_id
        row.status = "claimed"
        row.updated_at = now

    def _to_summary(self, row: RemoteTaskModel) -> RemoteTaskSummaryResponse:
        assignment = self._current_assignment(row)
        approval = self._latest_approval(row)
        latest_heartbeat = None
        if row.heartbeats:
            latest = max(row.heartbeats, key=lambda item: item.created_at)
            latest_heartbeat = RemoteTaskHeartbeatSummary(
                id=latest.id,
                actor_id=latest.actor_id,
                phase=latest.phase,
                summary=latest.summary,
                commands_run_count=latest.commands_run_count,
                files_read_count=latest.files_read_count,
                files_modified_count=latest.files_modified_count,
                tests_run_count=latest.tests_run_count,
                created_at=latest.created_at,
            )

        recent_evidence = sorted(row.evidence_items, key=lambda item: item.created_at, reverse=True)[:10]
        return RemoteTaskSummaryResponse(
            id=row.id,
            machine_id=row.machine_id,
            thread_id=row.thread_id,
            origin_surface=row.origin_surface,
            origin_message_id=row.origin_message_id,
            objective=row.objective,
            success_criteria=json.loads(row.success_criteria_json or "{}"),
            status=row.status,
            priority=row.priority,
            owner_actor_id=row.owner_actor_id,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            latest_approval=self._to_approval_summary(approval) if approval is not None else None,
            current_assignment=(
                RemoteTaskAssignmentSummary(
                    id=assignment.id,
                    actor_id=assignment.actor_id,
                    lease_token=assignment.lease_token,
                    lease_expires_at=assignment.lease_expires_at,
                    status=assignment.status,
                    claimed_at=assignment.claimed_at,
                    released_at=assignment.released_at,
                )
                if assignment is not None
                else None
            ),
            latest_heartbeat=latest_heartbeat,
            recent_evidence=[
                RemoteTaskEvidenceSummary(
                    id=item.id,
                    actor_id=item.actor_id,
                    kind=item.kind,
                    summary=item.summary,
                    payload=json.loads(item.payload_json or "{}"),
                    created_at=item.created_at,
                )
                for item in recent_evidence
            ],
        )

    def _to_approval_summary(self, approval: RemoteTaskApprovalModel) -> RemoteTaskApprovalSummary:
        return RemoteTaskApprovalSummary(
            id=approval.id,
            actor_id=approval.actor_id,
            reason=approval.reason,
            status=approval.status,
            note=approval.note,
            requested_at=approval.requested_at,
            resolved_at=approval.resolved_at,
            resolved_by=approval.resolved_by,
            resolution=approval.resolution,
        )

    def _to_note_summary(self, note: RemoteTaskNoteModel) -> RemoteTaskNoteSummary:
        return RemoteTaskNoteSummary(
            id=note.id,
            actor_id=note.actor_id,
            kind=note.kind,
            content=note.content,
            created_at=note.created_at,
        )
