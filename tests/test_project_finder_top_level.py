from __future__ import annotations

import sys
from pathlib import Path


OPS_CURE_ROOT = Path(r"C:\Users\darkh\Projects\ops-cure")
PC_LAUNCHER_ROOT = OPS_CURE_ROOT / "pc_launcher"

if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))

from config_loader import load_project  # noqa: E402
from project_finder import ProjectFinder  # noqa: E402


def _write_project(project_file: Path, root: Path) -> None:
    project_file.write_text(
        f"""
profile_name: SampleProfile
default_target_name: SampleProject
default_workdir: {root.as_posix()}
guild_id: "guild-1"
parent_channel_id: "parent-1"
allowed_user_ids:
  - "user-1"
bridge:
  base_url: http://127.0.0.1:8080
agents:
  - name: planner
    cli: claude
    role: planning
    prompt_file: prompts/planner.md
    default: true
finder:
  roots:
    - {root.as_posix()}
  max_depth: 1
  max_candidates: 8
""".strip(),
        encoding="utf-8",
    )


def test_project_finder_only_scans_top_level_directories(tmp_path):
    root = tmp_path / "Projects"
    top_level = root / "GenWorld"
    nested = top_level / "NestedCandidate"
    other = root / "UlalaCheese"
    top_level.mkdir(parents=True)
    nested.mkdir(parents=True)
    other.mkdir(parents=True)
    (nested / "project.godot").write_text('config/name="NestedCandidate"', encoding="utf-8")
    (top_level / "README.md").write_text("# GenWorld", encoding="utf-8")

    project_dir = tmp_path / "profile"
    prompts_dir = project_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "planner.md").write_text("Return JSON only.", encoding="utf-8")
    project_file = project_dir / "project.yaml"
    _write_project(project_file, root)

    config = load_project(project_file)
    finder = ProjectFinder(project_file=project_file, project=config)

    candidates = finder._discover_candidates("NestedCandidate")  # noqa: SLF001

    assert all(candidate.path != nested for candidate in candidates)
    assert all(candidate.path.parent == root for candidate in candidates)


def test_project_finder_resolves_only_root_children(tmp_path):
    root = tmp_path / "Projects"
    genworld = root / "GenWorld"
    nested = genworld / "Build"
    ulala = root / "UlalaCheese"
    genworld.mkdir(parents=True)
    nested.mkdir(parents=True)
    ulala.mkdir(parents=True)
    (genworld / "project.godot").write_text('config/name="GenWorld"', encoding="utf-8")
    (nested / "README.md").write_text("# Build", encoding="utf-8")

    project_dir = tmp_path / "profile"
    prompts_dir = project_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "planner.md").write_text("Return JSON only.", encoding="utf-8")
    project_file = project_dir / "project.yaml"
    _write_project(project_file, root)

    config = load_project(project_file)
    finder = ProjectFinder(project_file=project_file, project=config)

    candidates = finder._discover_candidates("GenWorld")  # noqa: SLF001

    assert [candidate.path for candidate in candidates] == [genworld]
