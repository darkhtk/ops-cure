from __future__ import annotations

import sys
from pathlib import Path


OPS_CURE_ROOT = Path(r"C:\Users\darkh\Projects\ops-cure")
PC_LAUNCHER_ROOT = OPS_CURE_ROOT / "pc_launcher"

if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))

from config_loader import load_project  # noqa: E402


def test_project_config_accepts_generic_profile_fields(tmp_path):
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
profile_name: GenericProfile
default_target_name: GenWorld
default_workdir: C:\\Projects\\GenWorld
guild_id: "guild-1"
parent_channel_id: "parent-1"
allowed_user_ids:
  - "user-1"
bridge:
  base_url: http://127.0.0.1:8080
agents:
  - name: coder
    cli: claude
    role: coding
    prompt_file: prompts/coder.md
    default: true
""".strip(),
        encoding="utf-8",
    )

    config = load_project(project_file)

    assert config.profile_name == "GenericProfile"
    assert config.project_name == "GenericProfile"
    assert config.resolved_default_target_name == "GenWorld"
    assert config.default_workdir == r"C:\Projects\GenWorld"
    assert config.workdir == r"C:\Projects\GenWorld"


def test_project_config_keeps_legacy_project_fields_compatible(tmp_path):
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_name: LegacyProfile
workdir: C:\\Projects\\LegacyProfile
guild_id: "guild-1"
parent_channel_id: "parent-1"
allowed_user_ids:
  - "user-1"
bridge:
  base_url: http://127.0.0.1:8080
agents:
  - name: coder
    cli: claude
    role: coding
    prompt_file: prompts/coder.md
    default: true
""".strip(),
        encoding="utf-8",
    )

    config = load_project(project_file)

    assert config.profile_name == "LegacyProfile"
    assert config.resolved_default_target_name == "LegacyProfile"
    assert config.default_workdir == r"C:\Projects\LegacyProfile"


def test_sample_profile_includes_curator_and_verifier_roles():
    sample_project = OPS_CURE_ROOT / "pc_launcher" / "projects" / "sample" / "project.yaml"

    config = load_project(sample_project)

    roles = {agent.name: agent.role for agent in config.agents}

    assert "curator" in roles
    assert roles["curator"] == "coordination"
    assert "verifier" in roles
    assert roles["verifier"] == "verification"
    assert config.policy.max_parallel_agents == 3
    assert config.verification.enabled is True
    assert {"smoke", "play_capture", "repro_bug"}.issubset(config.verification.commands.keys())
