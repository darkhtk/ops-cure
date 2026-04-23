"""Thin generic operation schemas for future kernel promotion.

These models intentionally stop at generic shape only. They do not define
persistence, product wording, browser UX, or runtime-specific execution rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

OperationStatus = Literal[
    "queued",
    "claimed",
    "executing",
    "verifying",
    "blocked",
    "interrupted",
    "completed",
    "failed",
    "stalled",
]


class OperationSummary(BaseModel):
    operation_id: str
    space_id: str | None = None
    subject_kind: str
    subject_id: str
    kind: str
    objective: str
    requested_by: str | None = None
    status: OperationStatus = "queued"
    created_at: datetime
    updated_at: datetime


class OperationAssignmentSummary(BaseModel):
    operation_id: str
    actor_id: str
    lease_id: str | None = None
    status: str = "claimed"
    claimed_at: datetime
    released_at: datetime | None = None


class OperationHeartbeatSummary(BaseModel):
    operation_id: str
    actor_id: str
    phase: str
    summary: str | None = None
    metrics: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    created_at: datetime


class ArtifactRefSummary(BaseModel):
    kind: str
    uri: str
    label: str | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class OperationEvidenceSummary(BaseModel):
    operation_id: str
    actor_id: str
    kind: str
    summary: str
    artifact: ArtifactRefSummary | None = None
    created_at: datetime
