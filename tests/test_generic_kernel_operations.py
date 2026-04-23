from __future__ import annotations

from datetime import datetime, timezone


def test_operation_schemas_stay_generic(app_env):
    from app.kernel.operations import (
        ArtifactRefSummary,
        OperationAssignmentSummary,
        OperationEvidenceSummary,
        OperationHeartbeatSummary,
        OperationSummary,
    )

    now = datetime.now(timezone.utc)
    operation = OperationSummary(
        operation_id="op-1",
        space_id="space-1",
        subject_kind="resource",
        subject_id="resource-1",
        kind="repair",
        objective="Stabilize the current subject.",
        requested_by="operator",
        status="queued",
        created_at=now,
        updated_at=now,
    )
    assignment = OperationAssignmentSummary(
        operation_id="op-1",
        actor_id="actor-1",
        lease_id="lease-1",
        status="claimed",
        claimed_at=now,
    )
    heartbeat = OperationHeartbeatSummary(
        operation_id="op-1",
        actor_id="actor-1",
        phase="executing",
        summary="Still making forward progress.",
        metrics={"steps": 3},
        created_at=now,
    )
    evidence = OperationEvidenceSummary(
        operation_id="op-1",
        actor_id="actor-1",
        kind="result",
        summary="Produced a generic artifact reference.",
        artifact=ArtifactRefSummary(kind="file", uri="artifact://reports/result.txt", label="result"),
        created_at=now,
    )

    assert operation.subject_kind == "resource"
    assert assignment.lease_id == "lease-1"
    assert heartbeat.metrics["steps"] == 3
    assert evidence.artifact is not None
    serialized = operation.model_dump()
    assert "machine_id" not in serialized
    assert "thread_id" not in serialized
    assert "browser_queue" not in serialized
    assert "codex_runtime" not in serialized
