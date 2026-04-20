from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select


def build_verification_manifest(schemas):
    return schemas.ProjectManifest(
        profile_name="GenericProfile",
        default_target_name="GenericProject",
        default_workdir=r"C:\Users\darkh\Projects\GenericProject",
        guild_id="guild-1",
        parent_channel_id="parent-1",
        allowed_user_ids=["user-1"],
        agents=[
            schemas.AgentManifest(
                name="planner",
                cli="claude",
                role="planning",
                prompt_file="prompts/planner.md",
                default=True,
            ),
        ],
        verification=schemas.VerificationManifest(
            enabled=True,
            provider="command",
            artifact_dir="_verification",
            run_timeout_seconds=120,
            commands={
                "smoke": [sys.executable, "-c", "print('smoke ok')"],
            },
            review=schemas.VerificationReviewConfig(require_operator_approval=True),
        ),
    )


async def _start_verified_session(app_env):
    manifest = build_verification_manifest(app_env.schemas)
    app_env.registry.register_projects("launcher-1", "host-1", [manifest])
    return await app_env.session_service.create_session_from_project(
        project_name="GenericProject",
        target_project_name="GenericProject",
        preset="GenericProfile",
        user_id="user-1",
        guild_id="guild-1",
        parent_channel_id="parent-1",
    )


def test_verification_queue_claim_complete_and_review(app_env):
    summary = __import__("asyncio").run(_start_verified_session(app_env))

    queued = __import__("asyncio").run(
        app_env.verification_service.enqueue_run(
            session_id=summary.id,
            mode="smoke",
            requested_by="user-1",
        ),
    )
    assert queued.status == "pending"
    assert queued.profile_name == "GenericProfile"

    claimed = __import__("asyncio").run(
        app_env.verification_service.claim_runs(
            launcher_id="launcher-1",
            capacity=1,
        ),
    )
    assert len(claimed) == 1
    assert claimed[0].mode == "smoke"
    assert claimed[0].command == [sys.executable, "-c", "print('smoke ok')"]

    completed = __import__("asyncio").run(
        app_env.verification_service.complete_run(
            run_id=queued.id,
            launcher_id="launcher-1",
            status="completed",
            summary_text="Smoke run passed.",
            error_text=None,
            artifacts=[
                app_env.schemas.VerifyArtifactInput(
                    artifact_type="screenshot",
                    label="desktop.png",
                    path=r"C:\tmp\desktop.png",
                ),
            ],
        ),
    )
    assert completed.status == "review_pending"
    assert completed.artifacts[0].artifact_type == "screenshot"
    assert app_env.thread_manager.messages
    assert "verification review_pending" in app_env.thread_manager.messages[-1][1]

    from app.models import SessionModel

    with app_env.db.session_scope() as db:
        session_row = db.scalar(select(SessionModel).where(SessionModel.id == summary.id))
        assert session_row is not None
        assert session_row.status_message_id is not None
        status_message = app_env.thread_manager.message_store[session_row.status_message_id][1]
    assert "Verification: smoke -> review_pending" in status_message
    assert "Attention: verification `smoke` is waiting for operator review" in status_message

    latest = __import__("asyncio").run(
        app_env.verification_service.latest_run(session_id=summary.id),
    )
    assert latest is not None
    assert latest.status == "review_pending"

    approved = __import__("asyncio").run(
        app_env.verification_service.review_latest(
            session_id=summary.id,
            decision="approved",
            reviewer="user-1",
            note="Looks good.",
        ),
    )
    assert approved.status == "approved"
    assert approved.latest_review is not None
    assert approved.latest_review.decision == "approved"


def test_command_verification_runner_collects_artifacts(tmp_path):
    repo_root = Path(r"C:\Users\darkh\Projects\ops-cure")
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from pc_launcher.config_loader import (
        AgentConfig,
        ArtifactConfig,
        BridgeProjectConfig,
        DiscordProjectConfig,
        ProjectConfig,
        StartupConfig,
        VerificationCaptureConfig,
        VerificationConfig,
        VerificationReviewConfig,
    )
    from pc_launcher.verification_runner import CommandVerificationRunner

    workdir = tmp_path / "project"
    workdir.mkdir()
    config = ProjectConfig(
        profile_name="RunnerProfile",
        default_target_name="RunnerTarget",
        default_workdir=str(workdir),
        guild_id="guild-1",
        parent_channel_id="parent-1",
        allowed_user_ids=["user-1"],
        bridge=BridgeProjectConfig(base_url="http://bridge", auth_token_env="BRIDGE_TOKEN"),
        agents=[
            AgentConfig(
                name="planner",
                cli="claude",
                role="planning",
                prompt_file="prompts/planner.md",
                default=True,
            ),
        ],
        discord=DiscordProjectConfig(),
        startup=StartupConfig(),
        artifacts=ArtifactConfig(),
        verification=VerificationConfig(
            enabled=True,
            provider="command",
            artifact_dir="_verification",
            run_timeout_seconds=120,
            commands={},
            capture=VerificationCaptureConfig(screenshots=False, video=False),
            review=VerificationReviewConfig(require_operator_approval=False),
        ),
    )
    runner = CommandVerificationRunner()
    run_payload = {
        "id": "verify-1",
        "session_id": "session-1",
        "mode": "smoke",
        "workdir": str(workdir),
        "artifact_dir": str(tmp_path / "artifacts"),
        "timeout_seconds": 120,
        "command": [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import os; "
                "verify_dir = Path(os.environ['OPS_CURE_VERIFY_DIR']); "
                "verify_dir.mkdir(parents=True, exist_ok=True); "
                "(verify_dir / 'proof.txt').write_text('ok', encoding='utf-8')"
            ),
        ],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    result = runner.run(run_payload=run_payload, project=config)

    assert result.status == "completed"
    labels = {artifact["label"] for artifact in result.artifacts}
    assert "proof.txt" in labels
    assert "result.json" in labels
