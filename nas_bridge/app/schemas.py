from __future__ import annotations

from datetime import datetime
from typing import Any

import ntpath
from pathlib import Path

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentManifest(BaseModel):
    name: str
    cli: str
    role: str
    prompt_file: str
    default: bool = False
    timeout_seconds: int = 900


class DiscordManifest(BaseModel):
    thread_name_template: str = "[{project_name}] {timestamp}"
    auto_archive_duration: int = 1440


class StartupManifest(BaseModel):
    send_ready_message: bool = True
    restore_last_session: bool = False
    open_tools: list[str] = Field(default_factory=list)


class FinderManifest(BaseModel):
    roots: list[str] = Field(default_factory=list)
    analyze_agent: str | None = None
    prompt_file: str | None = None
    max_depth: int = 4
    max_candidates: int = 12
    analysis_timeout_seconds: int = 120
    exclude_dirs: list[str] = Field(default_factory=list)


class PowerManifest(BaseModel):
    target: str = "default"
    provider: str = "noop"
    mac_address: str | None = None
    broadcast_ip: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ExecutionManifest(BaseModel):
    target: str = "default"
    provider: str = "windows_launcher"
    platform: str = "windows"
    launcher_id_hint: str | None = None
    host_pattern: str | None = None
    auto_start_expected: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)


class ProjectPolicy(BaseModel):
    max_parallel_agents: int = 1
    auto_retry: bool = True
    max_retries: int = 1
    quiet_discord: bool = True
    approval_mode: str = "critical_only"
    allow_cross_agent_handoff: bool = True

    @field_validator("max_parallel_agents")
    @classmethod
    def validate_parallel_agents(cls, value: int) -> int:
        return max(1, min(value, 8))

    @field_validator("max_retries")
    @classmethod
    def validate_max_retries(cls, value: int) -> int:
        return max(0, min(value, 10))


class VerificationCaptureConfig(BaseModel):
    screenshots: bool = True
    video: bool = False


class VerificationReviewConfig(BaseModel):
    require_operator_approval: bool = False


class VerificationManifest(BaseModel):
    enabled: bool = False
    provider: str = "command"
    artifact_dir: str = "_verification"
    run_timeout_seconds: int = 300
    auto_verify_on_handoff: bool = False
    commands: dict[str, list[str]] = Field(default_factory=dict)
    capture: VerificationCaptureConfig = Field(default_factory=VerificationCaptureConfig)
    review: VerificationReviewConfig = Field(default_factory=VerificationReviewConfig)


class ProjectManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_name: str = Field(validation_alias=AliasChoices("project_name", "profile_name"))
    default_target_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("default_target_name"),
    )
    workdir: str = Field(validation_alias=AliasChoices("workdir", "default_workdir"))
    guild_id: str
    parent_channel_id: str
    allowed_user_ids: list[str]
    discord: DiscordManifest = Field(default_factory=DiscordManifest)
    agents: list[AgentManifest]
    startup: StartupManifest = Field(default_factory=StartupManifest)
    finder: FinderManifest = Field(default_factory=FinderManifest)
    power: PowerManifest = Field(default_factory=PowerManifest)
    execution: ExecutionManifest = Field(default_factory=ExecutionManifest)
    policy: ProjectPolicy = Field(default_factory=ProjectPolicy)
    verification: VerificationManifest = Field(default_factory=VerificationManifest)

    @model_validator(mode="after")
    def validate_default_agent(self) -> "ProjectManifest":
        defaults = [agent for agent in self.agents if agent.default]
        if len(self.agents) > 1 and len(defaults) > 1:
            raise ValueError("Only one agent can be marked as default.")
        return self

    @property
    def profile_name(self) -> str:
        return self.project_name

    @property
    def default_workdir(self) -> str:
        return self.workdir

    @property
    def resolved_default_target_name(self) -> str:
        normalized = self.workdir.strip().rstrip("\\/")
        derived = ntpath.basename(normalized) or Path(normalized).name
        return (self.default_target_name or derived or self.project_name).strip()


class CatalogRegistrationRequest(BaseModel):
    launcher_id: str
    hostname: str
    projects: list[ProjectManifest]


class LaunchClaimRequest(BaseModel):
    launcher_id: str
    capacity: int = 10

    @field_validator("capacity")
    @classmethod
    def validate_capacity(cls, value: int) -> int:
        return max(1, min(value, 50))


class AgentStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_name: str
    cli_type: str
    role: str
    is_default: bool
    status: str
    desired_status: str = "ready"
    paused_reason: str | None = None
    last_heartbeat_at: datetime | None = None
    worker_id: str | None = None
    pid_hint: int | None = None
    current_activity_line: str | None = None
    current_activity_updated_at: datetime | None = None
    drift_state: str = "unknown"
    drift_reason: str | None = None
    workspace_ready: bool | None = None
    last_artifact_at: datetime | None = None
    last_artifact_path: str | None = None
    current_task_id: str | None = None
    current_task_state: str | None = None


class TranscriptContextEntry(BaseModel):
    direction: str
    actor: str
    content: str
    created_at: datetime


class ThreadDeltaEntry(BaseModel):
    cursor: str
    direction: str
    actor: str
    kind: str
    content: str
    task_id: str | None = None
    created_at: datetime


class ThreadDeltaRequest(BaseModel):
    session_id: str
    agent_name: str
    cursor: str | None = None
    kinds: list[str] = Field(default_factory=list)
    task_id: str | None = None
    limit: int = 12

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value: int) -> int:
        return max(1, min(value, 50))


class ThreadDeltaResponse(BaseModel):
    next_cursor: str | None = None
    events: list[ThreadDeltaEntry] = Field(default_factory=list)


class TaskStateSummary(BaseModel):
    id: str
    task_key: str
    title: str
    role: str
    assigned_agent: str | None = None
    source_agent: str | None = None
    depends_on_task_key: str | None = None
    semantic_scope: str | None = None
    file_scope: list[str] = Field(default_factory=list)
    state: str
    revision: int = 1
    session_epoch: int = 1
    summary_text: str | None = None
    body_text: str | None = None
    latest_brief_name: str | None = None
    latest_log_name: str | None = None
    created_at: datetime
    updated_at: datetime
    last_transition_at: datetime


class HandoffStateSummary(BaseModel):
    id: str
    task_id: str
    task_key: str
    source_agent: str
    target_agent: str
    target_role: str
    state: str
    revision: int = 1
    session_epoch: int = 1
    body_text: str
    created_at: datetime
    claimed_at: datetime | None = None
    consumed_at: datetime | None = None


class PowerTargetSummary(BaseModel):
    name: str
    provider: str
    state: str


class ExecutionTargetSummary(BaseModel):
    name: str
    provider: str
    platform: str
    state: str
    launcher_id: str | None = None
    auto_start_expected: bool = True


class SessionPolicyResponse(ProjectPolicy):
    source: str = "profile"
    version: int = 1
    updated_by: str = "system"
    updated_at: datetime | None = None


class SessionOperationResponse(BaseModel):
    id: str
    operation_type: str
    status: str
    requested_by: str
    created_at: datetime
    completed_at: datetime | None = None


class SessionLaunchResponse(BaseModel):
    session_id: str
    project_name: str
    target_project_name: str | None = None
    preset: str
    workdir: str
    status: str
    agents: list[AgentStatusResponse]


class WorkerRegisterRequest(BaseModel):
    session_id: str
    agent_name: str
    worker_id: str
    launcher_id: str
    pid_hint: int | None = None


class ArtifactHeartbeatSnapshot(BaseModel):
    workspace_ready: bool = False
    state_label: str | None = None
    state_updated_at: datetime | None = None
    current_task_state: str | None = None
    current_task_id: str | None = None
    current_task_updated_at: datetime | None = None
    latest_artifact_at: datetime | None = None
    latest_artifact_path: str | None = None


class WorkerHeartbeatRequest(BaseModel):
    session_id: str
    agent_name: str
    worker_id: str
    status: str
    pid_hint: int | None = None
    artifact_snapshot: ArtifactHeartbeatSnapshot | None = None
    activity_line: str | None = None


class JobPayload(BaseModel):
    id: str
    session_id: str
    agent_name: str
    job_type: str
    task_id: str | None = None
    task_revision: int = 0
    lease_token: str | None = None
    session_epoch: int = 1
    input_text: str
    user_id: str
    project_name: str
    session_title: str | None = None
    target_project_name: str | None = None
    preset: str | None = None
    session_status: str
    session_summary: str | None = None
    available_agents: list[AgentStatusResponse] = Field(default_factory=list)
    recent_transcript: list[TranscriptContextEntry] = Field(default_factory=list)
    source_discord_message_id: str | None = None
    created_at: datetime


class WorkerPollRequest(BaseModel):
    session_id: str
    agent_name: str
    worker_id: str


class WorkerPollResponse(BaseModel):
    job: JobPayload | None = None


class JobCompleteRequest(BaseModel):
    session_id: str
    agent_name: str
    worker_id: str
    output_text: str
    thread_output_text: str | None = None
    lease_token: str | None = None
    task_revision: int | None = None
    session_epoch: int | None = None
    pid_hint: int | None = None


class JobFailRequest(BaseModel):
    session_id: str
    agent_name: str
    worker_id: str
    error_text: str
    lease_token: str | None = None
    task_revision: int | None = None
    session_epoch: int | None = None
    pid_hint: int | None = None


class ProjectFindClaimRequest(BaseModel):
    launcher_id: str
    capacity: int = 1

    @field_validator("capacity")
    @classmethod
    def validate_capacity(cls, value: int) -> int:
        return max(1, min(value, 10))


class ProjectFindCandidate(BaseModel):
    path: str
    display_name: str
    rationale: str | None = None
    score: float | None = None


class ProjectFindLaunchResponse(BaseModel):
    id: str
    preset: str
    query_text: str
    requested_by: str
    guild_id: str
    parent_channel_id: str
    finder: FinderManifest
    created_at: datetime


class ProjectFindCompleteRequest(BaseModel):
    launcher_id: str
    status: str
    selected_path: str | None = None
    selected_name: str | None = None
    reason: str | None = None
    confidence: float | None = None
    candidates: list[ProjectFindCandidate] = Field(default_factory=list)
    error_text: str | None = None


class ProjectFindSummaryResponse(BaseModel):
    id: str
    preset: str
    query_text: str
    status: str
    requested_by: str
    guild_id: str
    parent_channel_id: str
    launcher_id: str | None = None
    selected_path: str | None = None
    selected_name: str | None = None
    reason: str | None = None
    confidence: float | None = None
    candidates: list[ProjectFindCandidate] = Field(default_factory=list)
    error_text: str | None = None
    session_id: str | None = None
    discord_thread_id: str | None = None
    created_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None


class SessionSummaryResponse(BaseModel):
    id: str
    project_name: str
    target_project_name: str | None = None
    preset: str | None = None
    discord_thread_id: str
    guild_id: str
    parent_channel_id: str
    workdir: str
    status: str
    desired_status: str = "ready"
    power_state: str = "unknown"
    execution_state: str = "unknown"
    pause_reason: str | None = None
    last_recovery_at: datetime | None = None
    last_recovery_reason: str | None = None
    created_by: str
    launcher_id: str | None = None
    session_epoch: int = 1
    created_at: datetime
    closed_at: datetime | None = None
    power_target: PowerTargetSummary | None = None
    execution_target: ExecutionTargetSummary | None = None
    policy: SessionPolicyResponse | None = None
    active_operation: SessionOperationResponse | None = None
    pending_jobs: int = 0
    active_jobs: int = 0
    tasks: list[TaskStateSummary] = Field(default_factory=list)
    queued_handoffs: list[HandoffStateSummary] = Field(default_factory=list)
    agents: list[AgentStatusResponse]


class PolicySetRequest(BaseModel):
    key: str
    value: str
    updated_by: str


class PolicySetResponse(BaseModel):
    session_id: str
    policy: SessionPolicyResponse


class SessionPauseResponse(BaseModel):
    session_id: str
    status: str
    desired_status: str
    pause_reason: str | None = None


class VerifyArtifactSummary(BaseModel):
    id: str
    artifact_type: str
    label: str
    path: str
    created_at: datetime


class ReviewDecisionSummary(BaseModel):
    id: str
    decision: str
    reviewer: str
    note: str | None = None
    created_at: datetime


class VerifyRunSummaryResponse(BaseModel):
    id: str
    session_id: str
    project_name: str
    target_project_name: str | None = None
    profile_name: str
    mode: str
    provider: str
    status: str
    requested_by: str
    launcher_id: str | None = None
    review_required: bool = False
    summary_text: str | None = None
    error_text: str | None = None
    artifact_dir: str
    created_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    reviewed_at: datetime | None = None
    artifacts: list[VerifyArtifactSummary] = Field(default_factory=list)
    latest_review: ReviewDecisionSummary | None = None


class VerifyRunRequest(BaseModel):
    mode: str
    requested_by: str


class VerifyClaimRequest(BaseModel):
    launcher_id: str
    capacity: int = 1

    @field_validator("capacity")
    @classmethod
    def validate_capacity(cls, value: int) -> int:
        return max(1, min(value, 10))


class VerifyArtifactInput(BaseModel):
    artifact_type: str
    label: str
    path: str


class VerifyRunClaimResponse(BaseModel):
    id: str
    session_id: str
    project_name: str
    target_project_name: str | None = None
    profile_name: str
    mode: str
    provider: str
    workdir: str
    artifact_dir: str
    timeout_seconds: int
    command: list[str]
    created_at: datetime


class VerifyRunCompleteRequest(BaseModel):
    launcher_id: str
    status: str
    summary_text: str | None = None
    error_text: str | None = None
    artifacts: list[VerifyArtifactInput] = Field(default_factory=list)


class VerifyReviewRequest(BaseModel):
    reviewer: str
    note: str | None = None


class RemoteTaskAssignmentSummary(BaseModel):
    id: str
    actor_id: str
    lease_token: str
    lease_expires_at: datetime
    status: str
    claimed_at: datetime
    released_at: datetime | None = None


class RemoteTaskHeartbeatSummary(BaseModel):
    id: str
    actor_id: str
    phase: str
    summary: str | None = None
    commands_run_count: int = 0
    files_read_count: int = 0
    files_modified_count: int = 0
    tests_run_count: int = 0
    created_at: datetime


class RemoteTaskEvidenceSummary(BaseModel):
    id: str
    actor_id: str
    kind: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RemoteTaskApprovalSummary(BaseModel):
    id: str
    actor_id: str
    reason: str
    status: str
    note: str | None = None
    requested_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution: str | None = None


class RemoteTaskNoteSummary(BaseModel):
    id: str
    actor_id: str
    kind: str
    content: str
    created_at: datetime


class RemoteTaskSummaryResponse(BaseModel):
    id: str
    machine_id: str
    thread_id: str
    origin_surface: str
    origin_message_id: str | None = None
    objective: str
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    status: str
    priority: str
    owner_actor_id: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    current_assignment: RemoteTaskAssignmentSummary | None = None
    latest_heartbeat: RemoteTaskHeartbeatSummary | None = None
    recent_evidence: list[RemoteTaskEvidenceSummary] = Field(default_factory=list)
    latest_approval: RemoteTaskApprovalSummary | None = None


class RemoteTaskCreateRequest(BaseModel):
    machine_id: str
    thread_id: str
    objective: str
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    origin_surface: str = "browser"
    origin_message_id: str | None = None
    priority: str = "normal"
    created_by: str = "browser"


class RemoteTaskClaimRequest(BaseModel):
    actor_id: str
    lease_seconds: int = 120

    @field_validator("lease_seconds")
    @classmethod
    def validate_lease_seconds(cls, value: int) -> int:
        return max(10, min(value, 3600))


class RemoteTaskClaimNextRequest(BaseModel):
    actor_id: str
    lease_seconds: int = 120
    exclude_origin_surfaces: list[str] = Field(default_factory=list)

    @field_validator("lease_seconds")
    @classmethod
    def validate_lease_seconds(cls, value: int) -> int:
        return max(10, min(value, 3600))

    @field_validator("exclude_origin_surfaces")
    @classmethod
    def validate_exclude_origin_surfaces(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item or "").strip().lower()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized


class RemoteTaskHeartbeatRequest(BaseModel):
    actor_id: str
    lease_token: str
    phase: str
    summary: str | None = None
    commands_run_count: int = 0
    files_read_count: int = 0
    files_modified_count: int = 0
    tests_run_count: int = 0
    lease_seconds: int = 120

    @field_validator(
        "commands_run_count",
        "files_read_count",
        "files_modified_count",
        "tests_run_count",
        "lease_seconds",
    )
    @classmethod
    def validate_non_negative(cls, value: int) -> int:
        return max(0, value)


class RemoteTaskEvidenceRequest(BaseModel):
    actor_id: str
    kind: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RemoteTaskCompleteRequest(BaseModel):
    actor_id: str
    lease_token: str
    summary: str | None = None


class RemoteTaskFailRequest(BaseModel):
    actor_id: str
    lease_token: str
    error_text: str


class RemoteTaskApprovalRequest(BaseModel):
    actor_id: str
    lease_token: str
    reason: str
    note: str | None = None


class RemoteTaskApprovalResolveRequest(BaseModel):
    resolved_by: str
    resolution: str
    note: str | None = None

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"approved", "denied"}:
            raise ValueError("resolution must be `approved` or `denied`.")
        return normalized


class RemoteTaskNoteRequest(BaseModel):
    actor_id: str
    kind: str = "note"
    content: str


class RemoteTaskInterruptRequest(BaseModel):
    actor_id: str
    lease_token: str
    note: str | None = None


class HealthResponse(BaseModel):
    status: str
    discord_enabled: bool
    discord_connected: bool
    active_launchers: int
    tracked_projects: int
    agents_in_drift: int = 0
    sessions_with_drift: int = 0
