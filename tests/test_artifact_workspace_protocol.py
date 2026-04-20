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

    payload = workspace.record_cli_result(
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

    assert "[[handoff agent=\"coder\"]]" in payload.control_text
    assert "OPS: type=handoff" in payload.thread_text
    assert "task=T-002" in payload.thread_text
    assert "from=planner" in payload.thread_text
    assert "to=coder" in payload.thread_text
    assert "read=CURRENT_STATE.md,TASKS/T-002.md" in payload.thread_text
    assert "HUMAN: QA harness setup has been handed to coder." in payload.thread_text
    assert "[[handoff" not in payload.thread_text


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


def test_record_cli_result_emits_answer_line_for_direct_operator_reply(tmp_path):
    project_root = tmp_path / "GenWorld"
    project_root.mkdir(parents=True, exist_ok=True)
    workspace = SessionWorkspace.create(
        project_workdir=project_root,
        artifacts=ArtifactConfig(sessions_dir="_discord_sessions", quiet_discord=True),
        session_name="GenWorld",
        session_id="session-12345678",
        agent_names=["planner", "coder", "reviewer"],
    )

    payload = workspace.record_cli_result(
        agent_name="planner",
        job_type="message",
        user_text="worker 상태 어때?",
        raw_output=(
            "[[answer]]지금 worker 3개가 모두 붙어 있고 planner가 첫 routing 작업을 처리 중이다.[[/answer]]\n"
            "[[report]]GenWorld 세션이 정상 시작됐고 에이전트들이 준비된 상태다.[[/report]]"
        ),
    )

    assert "OPS: type=answer" in payload.thread_text
    assert "ANSWER: 지금 worker 3개가 모두 붙어 있고 planner가 첫 routing 작업을 처리 중이다." in payload.thread_text
    assert "HUMAN: GenWorld 세션이 정상 시작됐고 에이전트들이 준비된 상태다." in payload.thread_text


def test_reconcile_from_bridge_summary_clears_stale_handoff_state(tmp_path):
    project_root = tmp_path / "GenWorld"
    project_root.mkdir(parents=True, exist_ok=True)
    workspace = SessionWorkspace.create(
        project_workdir=project_root,
        artifacts=ArtifactConfig(sessions_dir="_discord_sessions", quiet_discord=True),
        session_name="GenWorld",
        session_id="session-12345678",
        agent_names=["planner", "coder", "reviewer"],
    )

    workspace.record_cli_result(
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

    changed = workspace.reconcile_from_bridge_summary(
        {
            "status": "ready",
            "desired_status": "ready",
            "pending_jobs": 0,
            "active_jobs": 0,
            "agents": [
                {"agent_name": "planner", "status": "idle", "worker_id": "worker-planner"},
                {"agent_name": "coder", "status": "idle", "worker_id": "worker-coder"},
                {"agent_name": "reviewer", "status": "idle", "worker_id": "worker-reviewer"},
            ],
        },
        agent_name="coder",
    )

    assert changed is True
    state_text = workspace.state_file.read_text(encoding="utf-8")
    current_task_text = workspace.current_task_file.read_text(encoding="utf-8")
    handoffs_text = workspace.handoffs_file.read_text(encoding="utf-8")
    task_board_text = workspace.task_board_file.read_text(encoding="utf-8")

    assert "- Status: `ready`" in state_text
    assert "No active job. Local artifacts were synchronized from the bridge session summary." in current_task_text
    assert "`T-002` -> `coder`" in handoffs_text
    assert "## ready" in task_board_text
    assert "`T-001`" in task_board_text


def test_reconcile_from_bridge_summary_marks_active_task_and_consumes_handoff(tmp_path):
    project_root = tmp_path / "GenWorld"
    project_root.mkdir(parents=True, exist_ok=True)
    workspace = SessionWorkspace.create(
        project_workdir=project_root,
        artifacts=ArtifactConfig(sessions_dir="_discord_sessions", quiet_discord=True),
        session_name="GenWorld",
        session_id="session-12345678",
        agent_names=["planner", "coder", "reviewer"],
    )

    workspace.record_cli_result(
        agent_name="planner",
        job_type="orchestration",
        user_text="T-010 analyze and hand off implementation",
        raw_output=(
            "[[report]]Coder should run the playable QA check.[[/report]]\n"
            "[[handoff agent=\"coder\"]]\n"
            "T-011\n"
            "Target summary: Run the playable QA check.\n"
            "Read CURRENT_STATE.md and TASK_BOARD.md first.\n"
            "Files: build-and-run.ps1\n"
            "Done condition: STATUS.md updated with run results.\n"
            "[[/handoff]]"
        ),
    )

    changed = workspace.reconcile_from_bridge_summary(
        {
            "status": "resuming_jobs",
            "desired_status": "ready",
            "pending_jobs": 0,
            "active_jobs": 1,
            "agents": [
                {"agent_name": "planner", "status": "idle", "worker_id": "worker-planner"},
                {"agent_name": "coder", "status": "busy", "worker_id": "worker-coder"},
                {"agent_name": "reviewer", "status": "idle", "worker_id": "worker-reviewer"},
            ],
        },
        agent_name="coder",
    )

    assert changed is True
    state_text = workspace.state_file.read_text(encoding="utf-8")
    current_task_text = workspace.current_task_file.read_text(encoding="utf-8")
    handoffs_text = workspace.handoffs_file.read_text(encoding="utf-8")
    task_board_text = workspace.task_board_file.read_text(encoding="utf-8")

    assert "- Status: `in_progress`" in state_text
    assert "- Last owner: `coder`" in state_text
    assert "Bridge session summary says `coder` is working on `T-011`" in state_text
    assert "- Session state: `in_progress`" in current_task_text
    assert "- Last active agent: `coder`" in current_task_text
    assert "- Task ID: `T-011`" in current_task_text
    assert "Bridge session summary says `coder` is working on `T-011`." in current_task_text
    assert "`T-011`" not in handoffs_text
    assert "No internal handoffs are currently recorded." in handoffs_text or "`T-011`" not in handoffs_text
    assert "## in_progress" in task_board_text
    assert "`T-011` | owner=`coder`" in task_board_text


def test_record_cli_result_keeps_human_line_untruncated(tmp_path):
    project_root = tmp_path / "GenWorld"
    project_root.mkdir(parents=True, exist_ok=True)
    workspace = SessionWorkspace.create(
        project_workdir=project_root,
        artifacts=ArtifactConfig(sessions_dir="_discord_sessions", quiet_discord=True),
        session_name="GenWorld",
        session_id="session-12345678",
        agent_names=["planner", "coder", "reviewer"],
    )

    long_sentence = " ".join(["stable"] * 120)
    payload = workspace.record_cli_result(
        agent_name="planner",
        job_type="orchestration",
        user_text="T-001 analyze the current issue",
        raw_output=f"[[report]]{long_sentence}[[/report]]",
    )

    assert "HUMAN:" in payload.thread_text
    assert long_sentence in payload.thread_text
    assert "..." not in payload.thread_text
    assert "[truncated]" not in payload.thread_text


def test_record_cli_result_emits_discuss_open_event_lines(tmp_path):
    project_root = tmp_path / "GenWorld"
    project_root.mkdir(parents=True, exist_ok=True)
    workspace = SessionWorkspace.create(
        project_workdir=project_root,
        artifacts=ArtifactConfig(sessions_dir="_discord_sessions", quiet_discord=True),
        session_name="GenWorld",
        session_id="session-12345678",
        agent_names=["planner", "coder", "reviewer"],
    )

    payload = workspace.record_cli_result(
        agent_name="planner",
        job_type="discussion",
        user_text="T-021 inspect the anomaly",
        raw_output=(
            "[[discuss type=\"open\" ask=\"reviewer,coder\" anomaly=\"A-001\"]]"
            "Task state and board disagree. Check CURRENT_TASK.md, TASK_BOARD.md, and failed task roll-up."
            "[[/discuss]]\n"
            "[[report]]planner started a short anomaly discussion.[[/report]]"
        ),
    )

    assert "[[discuss type=\"open\"" in payload.control_text
    assert "OPS: type=discuss_open" in payload.thread_text
    assert "anomaly=A-001" in payload.thread_text
    assert "ask=reviewer,coder" in payload.thread_text
    assert "HUMAN: planner started a short anomaly discussion." in payload.thread_text
