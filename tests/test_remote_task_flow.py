from __future__ import annotations


def test_remote_task_service_create_claim_heartbeat_evidence_complete(app_env):
    from app.schemas import (
        RemoteTaskClaimRequest,
        RemoteTaskCompleteRequest,
        RemoteTaskCreateRequest,
        RemoteTaskEvidenceRequest,
        RemoteTaskHeartbeatRequest,
    )
    from app.services.remote_task_service import RemoteTaskService

    service = RemoteTaskService()
    created = service.create_task(
        RemoteTaskCreateRequest(
            machine_id="machine-a",
            thread_id="thread-a",
            objective="Make browser transcript submit feel immediate.",
            success_criteria={"browser": ["optimistic bubble", "queue row"]},
            created_by="browser-user",
        ),
    )

    assert created.status == "queued"
    assert created.machine_id == "machine-a"
    assert created.current_assignment is None

    listed = service.list_tasks(machine_id="machine-a")
    assert [item.id for item in listed] == [created.id]

    claimed = service.claim_task(
        created.id,
        RemoteTaskClaimRequest(actor_id="codex-homedev", lease_seconds=90),
    )
    assert claimed.status == "claimed"
    assert claimed.owner_actor_id == "codex-homedev"
    assert claimed.current_assignment is not None
    lease_token = claimed.current_assignment.lease_token

    no_evidence_heartbeat = service.heartbeat_task(
        created.id,
        RemoteTaskHeartbeatRequest(
            actor_id="codex-homedev",
            lease_token=lease_token,
            phase="executing",
            summary="I am supposedly working.",
            commands_run_count=0,
            files_read_count=0,
            files_modified_count=0,
            tests_run_count=0,
        ),
    )
    assert no_evidence_heartbeat.status == "claimed"
    assert no_evidence_heartbeat.latest_heartbeat is not None
    assert no_evidence_heartbeat.latest_heartbeat.phase == "executing"

    with_evidence = service.add_evidence(
        created.id,
        RemoteTaskEvidenceRequest(
            actor_id="codex-homedev",
            kind="file_write",
            summary="Patched the browser transcript component.",
            payload={"files": ["public/app.js"]},
        ),
    )
    assert with_evidence.status == "executing"
    assert with_evidence.recent_evidence[0].kind == "file_write"

    completed = service.complete_task(
        created.id,
        RemoteTaskCompleteRequest(
            actor_id="codex-homedev",
            lease_token=lease_token,
            summary="Transcript optimistic path shipped.",
        ),
    )
    assert completed.status == "completed"
    assert completed.recent_evidence[0].kind == "result"


def test_remote_task_service_fail_path_records_error_evidence(app_env):
    from app.schemas import RemoteTaskClaimRequest, RemoteTaskCreateRequest, RemoteTaskFailRequest
    from app.services.remote_task_service import RemoteTaskService

    service = RemoteTaskService()
    created = service.create_task(
        RemoteTaskCreateRequest(
            machine_id="machine-b",
            thread_id="thread-b",
            objective="Validate approval flow.",
            success_criteria={"browser": ["approval badge"]},
            created_by="browser-user",
        ),
    )
    claimed = service.claim_task(
        created.id,
        RemoteTaskClaimRequest(actor_id="codex-desktop", lease_seconds=120),
    )

    failed = service.fail_task(
        created.id,
        RemoteTaskFailRequest(
            actor_id="codex-desktop",
            lease_token=claimed.current_assignment.lease_token,
            error_text="approval UI contract missing",
        ),
    )

    assert failed.status == "failed"
    assert failed.recent_evidence[0].kind == "error"
    assert "approval UI contract missing" in failed.recent_evidence[0].summary


def test_remote_task_service_supports_approval_notes_and_interrupt(app_env):
    from app.schemas import (
        RemoteTaskApprovalRequest,
        RemoteTaskApprovalResolveRequest,
        RemoteTaskClaimRequest,
        RemoteTaskCreateRequest,
        RemoteTaskInterruptRequest,
        RemoteTaskNoteRequest,
    )
    from app.services.remote_task_service import RemoteTaskService

    service = RemoteTaskService()
    created = service.create_task(
        RemoteTaskCreateRequest(
            machine_id="machine-c",
            thread_id="thread-c",
            objective="Handle approval flow honestly.",
            success_criteria={"browser": ["approval badge", "blocked state"]},
            created_by="browser-user",
        ),
    )
    claimed = service.claim_task(
        created.id,
        RemoteTaskClaimRequest(actor_id="codex-reviewer", lease_seconds=120),
    )
    lease_token = claimed.current_assignment.lease_token

    noted = service.add_note(
        created.id,
        RemoteTaskNoteRequest(
            actor_id="codex-reviewer",
            kind="question",
            content="Need human confirmation before touching deployment state.",
        ),
    )
    assert noted.kind == "question"

    noted_list = service.list_notes(created.id)
    assert len(noted_list) == 1
    assert noted_list[0].content.startswith("Need human confirmation")

    blocked = service.request_approval(
        created.id,
        RemoteTaskApprovalRequest(
            actor_id="codex-reviewer",
            lease_token=lease_token,
            reason="Need approval before changing deployment-facing state.",
            note="This affects the browser surface.",
        ),
    )
    assert blocked.status == "blocked_approval"
    assert blocked.latest_approval is not None
    assert blocked.latest_approval.status == "pending"

    latest_approval = service.get_latest_approval(created.id)
    assert latest_approval is not None
    assert latest_approval.reason.startswith("Need approval")

    approved = service.resolve_approval(
        created.id,
        RemoteTaskApprovalResolveRequest(
            resolved_by="Semirain",
            resolution="approved",
            note="Proceed with the change.",
        ),
    )
    assert approved.status == "claimed"
    assert approved.latest_approval is not None
    assert approved.latest_approval.resolution == "approved"

    interrupted = service.interrupt_task(
        created.id,
        RemoteTaskInterruptRequest(
            actor_id="codex-reviewer",
            lease_token=lease_token,
            note="Stopping to let another device take over.",
        ),
    )
    assert interrupted.status == "interrupted"
    notes_after_interrupt = service.list_notes(created.id)
    assert notes_after_interrupt[-1].kind == "interrupt"
