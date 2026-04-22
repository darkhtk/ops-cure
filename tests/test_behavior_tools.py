from __future__ import annotations

import sys
from pathlib import Path

from conftest import OPS_CURE_ROOT


def test_load_chat_participant_behavior_manifest():
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    from pc_launcher.behavior_tools import load_behavior_manifest

    manifest = load_behavior_manifest("chat-participant", repo_root=OPS_CURE_ROOT)

    assert manifest.name == "chat-participant"
    assert manifest.runtime.runner_module == "pc_launcher.connectors.chat_participant.runner"
    assert manifest.runtime.sender_module == "pc_launcher.connectors.chat_participant.send_message"
    assert "client" in manifest.targets


def test_install_behavior_creates_chat_participant_project_and_env(tmp_path):
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    from pc_launcher.behavior_tools import install_behavior

    project_file = tmp_path / "pc_launcher" / "projects" / "chat_participant" / "project.yaml"
    env_file = tmp_path / "pc_launcher" / ".env"

    result = install_behavior(
        "chat-participant",
        repo_root=OPS_CURE_ROOT,
        project_file=project_file,
        env_file=env_file,
        bridge_url="https://example.test",
        workdir=r"C:\Users\darkh\Projects\ops-cure",
        install_requirements=False,
    )

    assert result.created_project is True
    assert result.created_env is True
    assert result.project_file.exists()
    assert result.env_file.exists()

    project_text = result.project_file.read_text(encoding="utf-8")
    assert "https://example.test" in project_text
    assert "auth_token_env: BRIDGE_TOKEN" in project_text
    assert r"default_workdir: C:\Users\darkh\Projects\ops-cure" in project_text


def test_build_behavior_commands_use_chat_participant_entrypoints():
    if str(OPS_CURE_ROOT) not in sys.path:
        sys.path.insert(0, str(OPS_CURE_ROOT))

    from pc_launcher.behavior_tools import build_behavior_run_command, build_behavior_send_command

    run_command = build_behavior_run_command(
        "chat-participant",
        repo_root=OPS_CURE_ROOT,
        thread_id="1496378989315489942",
        actor_name="codex-test",
        codex_thread_id="thread-123",
        run_once=True,
    )
    send_command = build_behavior_send_command(
        "chat-participant",
        repo_root=OPS_CURE_ROOT,
        thread_id="1496378989315489942",
        actor_name="codex-test",
        message_file=Path(r"C:\temp\message.txt"),
    )

    assert "pc_launcher.connectors.chat_participant.runner" in run_command
    assert "--codex-thread-id" in run_command
    assert "thread-123" in run_command
    assert "--once" in run_command

    assert "pc_launcher.connectors.chat_participant.send_message" in send_command
    assert "--message-file" in send_command
    assert r"C:\temp\message.txt" in send_command
