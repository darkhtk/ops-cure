from __future__ import annotations

import sys
from pathlib import Path


OPS_CURE_ROOT = Path(r"C:\Users\darkh\Projects\ops-cure")
PC_LAUNCHER_ROOT = OPS_CURE_ROOT / "pc_launcher"

if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))

from artifact_workspace import SessionWorkspace
from config_loader import ArtifactConfig


def test_record_cli_result_emits_async_bus_protocol(tmp_path):
    project_root = tmp_path / "GenWorld"
    project_root.mkdir(parents=True, exist_ok=True)
    workspace = SessionWorkspace.create(
        project_workdir=project_root,
        artifacts=ArtifactConfig(sessions_dir="_discord_sessions", quiet_discord=True),
        session_name="GenWorld",
        session_id="session-12345678",
        agent_names=["planner", "coder", "reviewer"],
    )

    message = workspace.record_cli_result(
        agent_name="planner",
        job_type="orchestration",
        user_text="T-001 analyze the current issue",
        raw_output=(
            "[[report]]QA harness setup has been handed to coder.[[/report]]\n"
            "[[handoff agent=\"coder\"]]\n"
            "T-002\n"
            "Target summary: Set up the playable QA harness.\n"
            "Read CURRENT_STATE.md and TASK_BOARD.md first.\n"
            "Files: tools/qa.py\n"
            "Done condition: Harness runs locally.\n"
            "[[/handoff]]"
        ),
    )

    assert "OPS: type=handoff" in message
    assert "task=T-002" in message
    assert "from=planner" in message
    assert "to=coder" in message
    assert "read=CURRENT_STATE.md,TASKS/T-002.md" in message
    assert "HUMAN: QA harness setup has been handed to coder." in message
    assert "[[handoff" not in message


def test_record_cli_failure_emits_async_bus_protocol(tmp_path):
    project_root = tmp_path / "GenWorld"
    project_root.mkdir(parents=True, exist_ok=True)
    workspace = SessionWorkspace.create(
        project_workdir=project_root,
        artifacts=ArtifactConfig(sessions_dir="_discord_sessions", quiet_discord=True),
        session_name="GenWorld",
        session_id="session-12345678",
        agent_names=["planner", "coder", "reviewer"],
    )

    message = workspace.record_cli_failure(
        agent_name="coder",
        job_type="handoff",
        user_text="T-003 run the playable QA harness",
        summary="Build entrypoint is missing, so the harness cannot start.",
        stdout_text="",
        stderr_text="missing executable",
        planner_recovery_expected=True,
    )

    assert "OPS: type=failed" in message
    assert "actor=coder" in message
    assert "task=T-003" in message
    assert "read=CURRENT_STATE.md,TASKS/T-003.md" in message
    assert "HUMAN: Build entrypoint is missing, so the harness cannot start." in message
    assert "ISSUE: planner_recovery_expected" in message
