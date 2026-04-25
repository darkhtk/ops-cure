from __future__ import annotations


def test_request_approval_mirrors_into_kernel_approvals(app_env):
    """Requesting approval on a remote task must also create a kernel
    approval row keyed off the same primary key, so the generic
    /api/kernel/approvals surface can serve the same record without an
    extra translation layer.
    """
    from app.kernel.approvals import APPROVAL_STATUS_PENDING, KernelApprovalService
    from app.schemas import (
        RemoteTaskApprovalRequest,
        RemoteTaskClaimRequest,
        RemoteTaskCreateRequest,
    )
    from app.services.remote_task_service import (
        REMOTE_TASK_APPROVAL_KIND,
        RemoteTaskService,
        remote_task_space_id,
    )

    service = RemoteTaskService()
    created = service.create_task(
        RemoteTaskCreateRequest(
            machine_id="m-mirror",
            thread_id="t-mirror",
            objective="Mirror approval into kernel.",
            created_by="browser",
        ),
    )
    claimed = service.claim_task(
        created.id,
        RemoteTaskClaimRequest(actor_id="codex-reviewer", lease_seconds=120),
    )
    blocked = service.request_approval(
        created.id,
        RemoteTaskApprovalRequest(
            actor_id="codex-reviewer",
            lease_token=claimed.current_assignment.lease_token,
            reason="Need confirmation before touching deploy state.",
        ),
    )

    approval_id = blocked.latest_approval.id
    kernel_service = KernelApprovalService()
    with app_env.db.session_scope() as db:
        record = kernel_service.get(db, approval_id=approval_id)

    assert record is not None
    assert record.id == approval_id
    assert record.space_id == remote_task_space_id(created.id)
    assert record.kind == REMOTE_TASK_APPROVAL_KIND
    assert record.status == APPROVAL_STATUS_PENDING
    assert record.payload.get("task_id") == created.id


def test_resolve_approval_mirrors_into_kernel_approvals(app_env):
    """Resolving the legacy approval row must drive the kernel record
    to a terminal status with the same resolution string.
    """
    from app.kernel.approvals import APPROVAL_STATUS_APPROVED, KernelApprovalService
    from app.schemas import (
        RemoteTaskApprovalRequest,
        RemoteTaskApprovalResolveRequest,
        RemoteTaskClaimRequest,
        RemoteTaskCreateRequest,
    )
    from app.services.remote_task_service import RemoteTaskService

    service = RemoteTaskService()
    created = service.create_task(
        RemoteTaskCreateRequest(
            machine_id="m-resolve",
            thread_id="t-resolve",
            objective="Resolve approval mirror.",
            created_by="browser",
        ),
    )
    claimed = service.claim_task(
        created.id,
        RemoteTaskClaimRequest(actor_id="codex-reviewer", lease_seconds=120),
    )
    service.request_approval(
        created.id,
        RemoteTaskApprovalRequest(
            actor_id="codex-reviewer",
            lease_token=claimed.current_assignment.lease_token,
            reason="Pre-deploy gate.",
        ),
    )
    resolved = service.resolve_approval(
        created.id,
        RemoteTaskApprovalResolveRequest(
            resolved_by="Semirain",
            resolution="approved",
            note="Looks good.",
        ),
    )

    approval_id = resolved.latest_approval.id
    kernel_service = KernelApprovalService()
    with app_env.db.session_scope() as db:
        record = kernel_service.get(db, approval_id=approval_id)

    assert record is not None
    assert record.status == APPROVAL_STATUS_APPROVED
    assert record.resolution == "approved"
    assert record.resolved_by == "Semirain"
    assert record.note == "Looks good."


def test_request_approval_mirror_failure_does_not_break_legacy_write(app_env):
    """If the kernel mirror raises, the legacy approval write must
    still succeed. The kernel side is best-effort during the migration
    window — it cannot destabilize the production approval flow.
    """
    from app.schemas import (
        RemoteTaskApprovalRequest,
        RemoteTaskClaimRequest,
        RemoteTaskCreateRequest,
    )
    from app.services.remote_task_service import RemoteTaskService

    class ExplodingApprovalService:
        def request(self, *args, **kwargs):
            raise RuntimeError("simulated kernel approval outage")

        def resolve(self, *args, **kwargs):
            raise RuntimeError("simulated kernel approval outage")

    service = RemoteTaskService(kernel_approval_service=ExplodingApprovalService())
    created = service.create_task(
        RemoteTaskCreateRequest(
            machine_id="m-isolated",
            thread_id="t-isolated",
            objective="Mirror failure isolation.",
            created_by="browser",
        ),
    )
    claimed = service.claim_task(
        created.id,
        RemoteTaskClaimRequest(actor_id="codex-reviewer", lease_seconds=120),
    )
    blocked = service.request_approval(
        created.id,
        RemoteTaskApprovalRequest(
            actor_id="codex-reviewer",
            lease_token=claimed.current_assignment.lease_token,
            reason="Mirror is broken but legacy must still queue this.",
        ),
    )

    assert blocked.status == "blocked_approval"
    assert blocked.latest_approval is not None
    assert blocked.latest_approval.status == "pending"
