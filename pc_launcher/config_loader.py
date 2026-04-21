from __future__ import annotations

import ntpath
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class DiscordProjectConfig(BaseModel):
    thread_name_template: str = "[{project_name}] {timestamp}"
    auto_archive_duration: int = 1440


class BridgeProjectConfig(BaseModel):
    base_url: str
    auth_token_env: str = "BRIDGE_TOKEN"


class AgentConfig(BaseModel):
    name: str
    cli: str
    role: str
    prompt_file: str
    default: bool = False
    timeout_seconds: int = 900


class StartupConfig(BaseModel):
    send_ready_message: bool = True
    restore_last_session: bool = False
    open_tools: list[str] = Field(default_factory=list)


class ArtifactConfig(BaseModel):
    sessions_dir: str = "_discord_sessions"
    quiet_discord: bool = True


class FinderConfig(BaseModel):
    roots: list[str] = Field(default_factory=list)
    analyze_agent: str | None = None
    prompt_file: str | None = None
    max_depth: int = 4
    max_candidates: int = 12
    analysis_timeout_seconds: int = 120
    exclude_dirs: list[str] = Field(
        default_factory=lambda: [
            ".git",
            ".godot",
            ".idea",
            ".venv",
            "__pycache__",
            "_discord_sessions",
            "Library",
            "Logs",
            "Temp",
            "build",
            "dist",
            "node_modules",
        ],
    )


class ProjectPolicy(BaseModel):
    max_parallel_agents: int = 1
    auto_retry: bool = True
    max_retries: int = 1
    quiet_discord: bool = True
    approval_mode: str = "critical_only"
    allow_cross_agent_handoff: bool = True


class VerificationCaptureConfig(BaseModel):
    screenshots: bool = True
    video: bool = False


class VerificationReviewConfig(BaseModel):
    require_operator_approval: bool = False


class VerificationConfig(BaseModel):
    enabled: bool = False
    provider: str = "command"
    artifact_dir: str = "_verification"
    run_timeout_seconds: int = 300
    auto_verify_on_handoff: bool = False
    commands: dict[str, list[str]] = Field(default_factory=dict)
    capture: VerificationCaptureConfig = Field(default_factory=VerificationCaptureConfig)
    review: VerificationReviewConfig = Field(default_factory=VerificationReviewConfig)


class ProjectConfig(BaseModel):
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
    discord: DiscordProjectConfig = Field(default_factory=DiscordProjectConfig)
    bridge: BridgeProjectConfig
    agents: list[AgentConfig]
    startup: StartupConfig = Field(default_factory=StartupConfig)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)
    finder: FinderConfig = Field(default_factory=FinderConfig)
    policy: ProjectPolicy = Field(default_factory=ProjectPolicy)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)

    @model_validator(mode="after")
    def validate_defaults(self) -> "ProjectConfig":
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

    def prompt_path_for(self, agent: AgentConfig, project_file: Path) -> Path:
        return (project_file.parent / agent.prompt_file).resolve()

    def prompt_text_for(self, agent: AgentConfig, project_file: Path) -> str:
        return self.prompt_path_for(agent, project_file).read_text(encoding="utf-8")

    def to_bridge_manifest(self) -> dict[str, object]:
        return {
            "profile_name": self.profile_name,
            "default_target_name": self.resolved_default_target_name,
            "default_workdir": self.default_workdir,
            "project_name": self.project_name,
            "workdir": self.workdir,
            "guild_id": self.guild_id,
            "parent_channel_id": self.parent_channel_id,
            "allowed_user_ids": self.allowed_user_ids,
            "discord": self.discord.model_dump(),
            "agents": [agent.model_dump() for agent in self.agents],
            "startup": self.startup.model_dump(),
            "finder": self.finder.model_dump(),
            "policy": self.policy.model_dump(),
            "verification": self.verification.model_dump(),
        }


def load_project(project_file: str | Path) -> ProjectConfig:
    project_path = Path(project_file).resolve()
    data = yaml.safe_load(project_path.read_text(encoding="utf-8")) or {}
    return ProjectConfig.model_validate(data)


def discover_project_files(projects_dir: str | Path) -> list[Path]:
    root = Path(projects_dir).resolve()
    return sorted(root.glob("*/project.yaml"))


def discover_projects(projects_dir: str | Path) -> list[tuple[Path, ProjectConfig]]:
    discovered: list[tuple[Path, ProjectConfig]] = []
    for project_file in discover_project_files(projects_dir):
        discovered.append((project_file, load_project(project_file)))
    return discovered


def find_agent(config: ProjectConfig, agent_name: str) -> AgentConfig:
    for agent in config.agents:
        if agent.name == agent_name:
            return agent
    raise ValueError(f"Agent '{agent_name}' is not configured for profile '{config.profile_name}'.")
