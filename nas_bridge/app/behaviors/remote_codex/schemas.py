"""Shared schemas and constants for the remote_codex behavior facade."""

from __future__ import annotations

from ...schemas import (
    RemoteTaskApprovalRequest,
    RemoteTaskApprovalResolveRequest,
    RemoteTaskApprovalSummary,
    RemoteTaskClaimNextRequest,
    RemoteTaskClaimRequest,
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

REMOTE_CODEX_TASK_STATUSES = (
    "queued",
    "claimed",
    "executing",
    "verifying",
    "blocked_approval",
    "interrupted",
    "completed",
    "failed",
)

REMOTE_CODEX_ACTIVE_TASK_STATUSES = (
    "queued",
    "claimed",
    "executing",
    "verifying",
    "blocked_approval",
)

__all__ = [
    "REMOTE_CODEX_ACTIVE_TASK_STATUSES",
    "REMOTE_CODEX_TASK_STATUSES",
    "RemoteTaskApprovalRequest",
    "RemoteTaskApprovalResolveRequest",
    "RemoteTaskApprovalSummary",
    "RemoteTaskClaimNextRequest",
    "RemoteTaskClaimRequest",
    "RemoteTaskCompleteRequest",
    "RemoteTaskCreateRequest",
    "RemoteTaskEvidenceRequest",
    "RemoteTaskEvidenceSummary",
    "RemoteTaskFailRequest",
    "RemoteTaskHeartbeatRequest",
    "RemoteTaskHeartbeatSummary",
    "RemoteTaskInterruptRequest",
    "RemoteTaskNoteRequest",
    "RemoteTaskNoteSummary",
    "RemoteTaskSummaryResponse",
]
