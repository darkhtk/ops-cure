from __future__ import annotations

from datetime import datetime

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


class JobPayload(BaseModel):
    id: str
    session_id: str
    agent_name: str
    job_type: str
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
    pid_hint: int | None = None


class JobFailRequest(BaseModel):
    session_id: str
    agent_name: str
    worker_id: str
    error_text: str
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
    created_at: datetime
    closed_at: datetime | None = None
    power_target: PowerTargetSummary | None = None
    execution_target: ExecutionTargetSummary | None = None
    policy: SessionPolicyResponse | None = None
    active_operation: SessionOperationResponse | None = None
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


class HealthResponse(BaseModel):
    status: str
    discord_enabled: bool
    discord_connected: bool
    active_launchers: int
    tracked_projects: int
    agents_in_drift: int = 0
    sessions_with_drift: int = 0
