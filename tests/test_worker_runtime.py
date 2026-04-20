from __future__ import annotations

from pathlib import Path

import yaml


class DummyAdapter:
    def build_command(self) -> list[str]:
        return ["python", "-c", "print('ok')"]

    def prepare_input(self, context):  # pragma: no cover - constructor test only
        del context
        return ""

    def combine_output(self, stdout: str, stderr: str, returncode: int) -> str:  # pragma: no cover
        del stderr, returncode
        return stdout


def test_worker_runtime_applies_workdir_override_to_mutable_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "test-token")

    project_dir = tmp_path / "sample"
    prompts_dir = project_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "planner.md").write_text("planner prompt", encoding="utf-8")

    project_file = project_dir / "project.yaml"
    project_file.write_text(
        yaml.safe_dump(
            {
                "profile_name": "sample",
                "default_target_name": "sample",
                "default_workdir": str(tmp_path / "original"),
                "guild_id": "guild-1",
                "parent_channel_id": "parent-1",
                "allowed_user_ids": ["user-1"],
                "bridge": {
                    "base_url": "http://127.0.0.1:18080",
                    "auth_token_env": "BRIDGE_TOKEN",
                },
                "agents": [
                    {
                        "name": "planner",
                        "cli": "claude",
                        "role": "planning",
                        "prompt_file": "prompts/planner.md",
                        "default": True,
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    from pc_launcher import worker_runtime as worker_runtime_module

    monkeypatch.setattr(worker_runtime_module, "get_adapter", lambda cli: DummyAdapter())

    override_dir = tmp_path / "override"
    runtime = worker_runtime_module.WorkerRuntime(
        project_file=project_file,
        session_id="session-1",
        agent_name="planner",
        launcher_id="homedev",
        workdir_override=str(override_dir),
    )

    assert runtime.project.workdir == str(Path(override_dir).resolve())
    assert runtime.project.default_workdir == str(Path(override_dir).resolve())

