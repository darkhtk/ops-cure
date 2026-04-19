from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml
from pydantic import BaseModel, Field, model_validator


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


class ProjectConfig(BaseModel):
    project_name: str
    workdir: str
    guild_id: str
    parent_channel_id: str
    allowed_user_ids: list[str]
    discord: DiscordProjectConfig = Field(default_factory=DiscordProjectConfig)
    bridge: BridgeProjectConfig
    agents: list[AgentConfig]
    startup: StartupConfig = Field(default_factory=StartupConfig)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)
    finder: FinderConfig = Field(default_factory=FinderConfig)

    @model_validator(mode="after")
    def validate_defaults(self) -> "ProjectConfig":
        defaults = [agent for agent in self.agents if agent.default]
        if len(self.agents) > 1 and len(defaults) > 1:
            raise ValueError("Only one agent can be marked as default.")
        return self

    def prompt_path_for(self, agent: AgentConfig, project_file: Path) -> Path:
        return (project_file.parent / agent.prompt_file).resolve()

    def prompt_text_for(self, agent: AgentConfig, project_file: Path) -> str:
        return self.prompt_path_for(agent, project_file).read_text(encoding="utf-8")

    def to_bridge_manifest(self) -> dict[str, object]:
        return {
            "project_name": self.project_name,
            "workdir": self.workdir,
            "guild_id": self.guild_id,
            "parent_channel_id": self.parent_channel_id,
            "allowed_user_ids": self.allowed_user_ids,
            "discord": self.discord.model_dump(),
            "agents": [agent.model_dump() for agent in self.agents],
            "startup": self.startup.model_dump(),
            "finder": self.finder.model_dump(),
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
    raise ValueError(f"Agent '{agent_name}' is not configured for project '{config.project_name}'.")
