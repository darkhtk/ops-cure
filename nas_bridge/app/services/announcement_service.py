from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import discord
from sqlalchemy import func, select

from ..db import session_scope
from ..models import JobModel, SessionModel, VerifyRunModel
from ..schemas import SessionSummaryResponse
from ..thread_manager import ThreadManager


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ATTACHED_HEARTBEAT_GRACE_SECONDS = 120


@dataclass(slots=True)
class SessionStatusSnapshot:
    session_id: str
    thread_id: str
    session_title: str
    target_project_name: str
    profile_name: str
    workdir: str
    status: str
    desired_status: str
    launcher_id: str
    power_line: str
    execution_line: str
    worker_summary: str
    active_worker_lines: tuple[str, ...]
    activity_report_line: str | None
    job_summary: str
    active_operation: str
    recovery_summary: str
    policy_summary: str
    attention: str
    next_action: str
    review_summary: str
    state_hash: str


class AnnouncementService:
    def __init__(self, *, thread_manager: ThreadManager) -> None:
        self.thread_manager = thread_manager
        self._summary_provider: Callable[[str], Awaitable[SessionSummaryResponse]] | None = None

    def bind_summary_provider(
        self,
        summary_provider: Callable[[str], Awaitable[SessionSummaryResponse]],
    ) -> None:
        self._summary_provider = summary_provider

    async def sync_session_status(self, session_id: str, *, force: bool = False) -> str | None:
        snapshot = await self._build_snapshot(session_id)
        if snapshot is None:
            return None

        existing_message_id: str | None = None
        with session_scope() as db:
            session_row = db.scalar(select(SessionModel).where(SessionModel.id == session_id))
            if session_row is None:
                return None

            if (
                not force
                and session_row.status_message_id
                and session_row.last_announced_state_hash == snapshot.state_hash
            ):
                return session_row.status_message_id
            existing_message_id = session_row.status_message_id

        embed = self._build_status_embed(snapshot)
        sent: tuple[str, str] | None = None
        if existing_message_id:
            sent = await self.thread_manager.edit_embed_message(
                snapshot.thread_id,
                existing_message_id,
                embed=embed,
                content=None,
            )
        if sent is None:
            sent = await self.thread_manager.post_embed_message(
                snapshot.thread_id,
                embed=embed,
                content=None,
            )
        if sent is None:
            return existing_message_id

        with session_scope() as db:
            session_row = db.scalar(select(SessionModel).where(SessionModel.id == session_id))
            if session_row is None:
                return sent[0]
            session_row.status_message_id = sent[0]
            session_row.last_announced_state_hash = snapshot.state_hash
            session_row.last_announced_at = utcnow()
            return session_row.status_message_id

    async def render_session_status_text(self, session_id: str) -> str:
        snapshot = await self._build_snapshot(session_id)
        if snapshot is None:
            raise ValueError("Session not found.")
        return self._render_status_card(snapshot)

    async def _build_snapshot(self, session_id: str) -> SessionStatusSnapshot | None:
        if self._summary_provider is None:
            raise RuntimeError("AnnouncementService summary provider is not configured.")

        summary = await self._summary_provider(session_id)
        with session_scope() as db:
            pending_jobs = int(
                db.scalar(
                    select(func.count())
                    .select_from(JobModel)
                    .where(JobModel.session_id == session_id)
                    .where(JobModel.status == "pending"),
                )
                or 0,
            )
            active_jobs = int(
                db.scalar(
                    select(func.count())
                    .select_from(JobModel)
                    .where(JobModel.session_id == session_id)
                    .where(JobModel.status == "in_progress"),
                )
                or 0,
            )
            active_job_rows = list(
                db.execute(
                    select(
                        JobModel.agent_name,
                        JobModel.task_id,
                        JobModel.job_type,
                    )
                    .where(JobModel.session_id == session_id)
                    .where(JobModel.status == "in_progress"),
                    )
                .all()
            )
            latest_verify = db.scalar(
                select(VerifyRunModel)
                .where(VerifyRunModel.session_id == session_id)
                .order_by(VerifyRunModel.created_at.desc()),
            )
            latest_verify_payload = None
            if latest_verify is not None:
                latest_verify_payload = {
                    "mode": latest_verify.mode,
                    "status": latest_verify.status,
                    "review_required": latest_verify.review_required,
                }

        now = utcnow()
        attached_workers = sum(1 for agent in summary.agents if self._is_agent_attached(agent, now))
        total_agents = len(summary.agents)
        active_job_lookup = {
            row.agent_name: {
                "task_id": row.task_id,
                "job_type": row.job_type,
            }
            for row in active_job_rows
        }
        active_agent_names = {
            agent.agent_name
            for agent in summary.agents
            if agent.status == "busy"
        }
        active_agent_names.update(active_job_lookup.keys())
        worker_summary = f"attached={attached_workers}/{total_agents}, active={len(active_agent_names)}"
        active_worker_lines = tuple(
            self._render_active_worker_line(agent, active_job_lookup.get(agent.agent_name))
            for agent in sorted(summary.agents, key=lambda item: item.agent_name)
            if agent.agent_name in active_agent_names
        )
        activity_report_line = None
        if active_worker_lines:
            activity_report_line = utcnow().astimezone().strftime("Live update: %H:%M")
        job_summary = f"pending={pending_jobs}, active={active_jobs}"

        power_line = "not configured"
        if summary.power_target is not None:
            power_line = (
                f"{summary.power_target.name} [{summary.power_target.provider}] "
                f"state={summary.power_target.state}"
            )

        execution_line = "not configured"
        if summary.execution_target is not None:
            execution_line = (
                f"{summary.execution_target.name} [{summary.execution_target.provider}] "
                f"state={summary.execution_target.state}"
            )
            if summary.execution_target.launcher_id:
                execution_line += f" launcher={summary.execution_target.launcher_id}"
        elif summary.launcher_id:
            execution_line = f"launcher={summary.launcher_id}"

        active_operation = "none"
        if summary.active_operation is not None:
            active_operation = (
                f"{summary.active_operation.operation_type} [{summary.active_operation.status}] "
                f"by {summary.active_operation.requested_by}"
            )

        recovery_summary = "none"
        if summary.last_recovery_reason:
            when = summary.last_recovery_at.isoformat() if summary.last_recovery_at else "n/a"
            recovery_summary = f"{summary.last_recovery_reason} @ {when}"

        policy_summary = "policy unavailable"
        if summary.policy is not None:
            policy_summary = (
                f"parallel={summary.policy.max_parallel_agents}, "
                f"auto_retry={summary.policy.auto_retry}, "
                f"max_retries={summary.policy.max_retries}, "
                f"approval={summary.policy.approval_mode}, "
                f"quiet={summary.policy.quiet_discord}"
            )

        review_summary = "none"
        if latest_verify_payload is not None:
            review_summary = f"{latest_verify_payload['mode']} -> {latest_verify_payload['status']}"

        attention = self._build_attention(
            summary=summary,
            latest_verify=latest_verify_payload,
            attached_workers=attached_workers,
            total_agents=total_agents,
            active_jobs=active_jobs,
        )
        next_action = self._build_next_action(
            summary=summary,
            attached_workers=attached_workers,
            total_agents=total_agents,
            active_jobs=active_jobs,
        )

        hash_payload = {
            "status": summary.status,
            "desired_status": summary.desired_status,
            "launcher_id": summary.launcher_id,
            "power_state": summary.power_state,
            "execution_state": summary.execution_state,
            "attached_workers": attached_workers,
            "total_agents": total_agents,
            "agent_states": [
                (
                    agent.agent_name,
                    agent.status,
                    agent.worker_id,
                    agent.current_activity_line,
                )
                for agent in sorted(summary.agents, key=lambda item: item.agent_name)
            ],
            "activity_report_line": activity_report_line,
            "pending_jobs": pending_jobs,
            "active_jobs": active_jobs,
            "pause_reason": summary.pause_reason,
            "last_recovery_reason": summary.last_recovery_reason,
            "active_operation": (
                summary.active_operation.operation_type if summary.active_operation is not None else None
            ),
            "policy": (
                summary.policy.max_parallel_agents,
                summary.policy.auto_retry,
                summary.policy.max_retries,
                summary.policy.quiet_discord,
                summary.policy.approval_mode,
                summary.policy.allow_cross_agent_handoff,
            )
            if summary.policy is not None
            else None,
            "latest_verify": (
                latest_verify_payload["mode"],
                latest_verify_payload["status"],
                latest_verify_payload["review_required"],
            )
            if latest_verify_payload is not None
            else None,
        }
        state_hash = hashlib.sha256(
            json.dumps(hash_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        ).hexdigest()

        return SessionStatusSnapshot(
            session_id=summary.id,
            thread_id=summary.discord_thread_id,
            session_title=summary.project_name,
            target_project_name=summary.target_project_name or summary.project_name,
            profile_name=summary.preset or "unknown",
            workdir=summary.workdir,
            status=summary.status,
            desired_status=summary.desired_status,
            launcher_id=summary.launcher_id or "unclaimed",
            power_line=power_line,
            execution_line=execution_line,
            worker_summary=worker_summary,
            active_worker_lines=active_worker_lines,
            activity_report_line=activity_report_line,
            job_summary=job_summary,
            active_operation=active_operation,
            recovery_summary=recovery_summary,
            policy_summary=policy_summary,
            attention=attention,
            next_action=next_action,
            review_summary=review_summary,
            state_hash=state_hash,
        )

    @staticmethod
    def _build_attention(
        *,
        summary: SessionSummaryResponse,
        latest_verify: dict[str, object] | None,
        attached_workers: int,
        total_agents: int,
        active_jobs: int,
    ) -> str:
        partially_attached = 0 < attached_workers < total_agents
        if summary.status == "failed_start":
            return summary.pause_reason or "startup failed before workers attached"
        if summary.pause_reason:
            return summary.pause_reason
        if latest_verify is not None and latest_verify["status"] == "review_pending":
            return f"verification `{latest_verify['mode']}` is waiting for operator review"
        if summary.status == "awaiting_launcher":
            return "execution plane is offline or launcher has not reconnected yet"
        if summary.status == "waiting_for_workers":
            if partially_attached:
                return (
                    f"{attached_workers}/{total_agents} workers are sending heartbeats, "
                    "but bridge attachment is still incomplete"
                )
            return "launcher is online but workers have not all attached yet"
        if partially_attached:
            if active_jobs > 0:
                return (
                    f"{attached_workers}/{total_agents} workers are attached and work has started, "
                    "but startup is still settling"
                )
            return f"{attached_workers}/{total_agents} workers are attached while the rest are still starting"
        return "none"

    @staticmethod
    def _build_next_action(
        *,
        summary: SessionSummaryResponse,
        attached_workers: int,
        total_agents: int,
        active_jobs: int,
    ) -> str:
        partially_attached = 0 < attached_workers < total_agents
        if summary.status == "awaiting_launcher":
            return "waiting for launcher reconnect"
        if summary.status == "waiting_for_workers":
            if partially_attached:
                return f"waiting for remaining workers to attach ({attached_workers}/{total_agents})"
            return f"waiting for workers to attach ({attached_workers}/{total_agents})"
        if partially_attached:
            if active_jobs > 0:
                return "work has started; keep watching attachment and worker registration"
            return f"waiting for remaining workers while startup continues ({attached_workers}/{total_agents})"
        if summary.status == "paused" or summary.desired_status == "paused":
            return "waiting for `/project resume`"
        if summary.status == "failed_start":
            return "start a fresh session after checking the execution plane"
        if summary.status == "ready":
            return "waiting for your next instruction"
        if summary.status == "closed":
            return "session is closed"
        return "reconciling session state"

    @staticmethod
    def _is_agent_attached(agent, now: datetime) -> bool:
        if agent.worker_id:
            return True
        if agent.last_heartbeat_at is None:
            return False
        heartbeat_at = agent.last_heartbeat_at
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        return (now - heartbeat_at) <= timedelta(seconds=ATTACHED_HEARTBEAT_GRACE_SECONDS)

    @staticmethod
    def _render_active_worker_line(agent, active_job: dict[str, str] | None = None) -> str:
        detail = agent.current_activity_line
        if not detail and agent.current_task_id:
            detail = f"working on {agent.current_task_id}"
        if not detail and active_job is not None:
            task_id = active_job.get("task_id")
            job_type = active_job.get("job_type") or "work"
            if task_id:
                detail = f"working on {task_id} ({job_type})"
            else:
                detail = f"working on {job_type}"
        if not detail:
            detail = "working"
        return f"- `{agent.agent_name}` [{agent.cli_type}] {detail}"

    @staticmethod
    def _render_status_card(snapshot: SessionStatusSnapshot) -> str:
        if snapshot.active_worker_lines:
            activity_block = (
                f"Active workers:\n"
                f"{chr(10).join(snapshot.active_worker_lines)}\n"
                f"{snapshot.activity_report_line or ''}\n"
            )
        else:
            activity_block = "Activity: waiting\n"
        return (
            "**Opscure Status**\n"
            f"Session: `{snapshot.session_title}`\n"
            f"Target: `{snapshot.target_project_name}`\n"
            f"Profile: `{snapshot.profile_name}`\n"
            f"Workdir: `{snapshot.workdir}`\n"
            f"State: `{snapshot.status}` (desired `{snapshot.desired_status}`)\n"
            f"Launcher: `{snapshot.launcher_id}`\n"
            f"Power: {snapshot.power_line}\n"
            f"Execution: {snapshot.execution_line}\n"
            f"Workers: {snapshot.worker_summary}\n"
            f"{activity_block}"
            f"Queue: {snapshot.job_summary}\n"
            f"Active op: {snapshot.active_operation}\n"
            f"Recovery: {snapshot.recovery_summary}\n"
            f"Verification: {snapshot.review_summary}\n"
            f"Policy: {snapshot.policy_summary}\n"
            f"Attention: {snapshot.attention}\n"
            f"Next: {snapshot.next_action}"
        )

    @staticmethod
    def _status_color(snapshot: SessionStatusSnapshot) -> discord.Color:
        attention = snapshot.attention.lower()
        if snapshot.status in {"failed_start"} or "issue" in attention or "blocked" in attention:
            return discord.Color.red()
        if "incomplete" in attention or "still settling" in attention:
            return discord.Color.orange()
        if snapshot.active_worker_lines:
            return discord.Color.blurple()
        if snapshot.status in {"ready", "closed"}:
            return discord.Color.green()
        if snapshot.status in {"paused", "awaiting_launcher", "waiting_for_workers"}:
            return discord.Color.orange()
        return discord.Color.light_grey()

    @classmethod
    def _build_status_embed(cls, snapshot: SessionStatusSnapshot) -> discord.Embed:
        workers_value = "\n".join(snapshot.active_worker_lines) if snapshot.active_worker_lines else "대기 중\n현재 활성 작업 없음"
        if snapshot.active_worker_lines and snapshot.activity_report_line:
            workers_value = f"{workers_value}\n{snapshot.activity_report_line}"

        embed = discord.Embed(
            title="Opscure 상태",
            description=(
                f"세션 `{snapshot.session_title}`\n"
                f"대상 `{snapshot.target_project_name}`\n"
                f"프로필 `{snapshot.profile_name}`"
            ),
            color=cls._status_color(snapshot),
            timestamp=utcnow(),
        )
        embed.add_field(
            name="현재 상태",
            value=(
                f"상태: `{snapshot.status}`\n"
                f"희망 상태: `{snapshot.desired_status}`\n"
                f"런처: `{snapshot.launcher_id}`\n"
                f"작업 디렉터리: `{snapshot.workdir}`"
            ),
            inline=False,
        )
        embed.add_field(name="활성 작업자", value=workers_value, inline=False)
        embed.add_field(
            name="큐와 검증",
            value=(
                f"작업자 연결: {snapshot.worker_summary}\n"
                f"큐: {snapshot.job_summary}\n"
                f"검증: {snapshot.review_summary}"
            ),
            inline=False,
        )
        embed.add_field(name="주의", value=snapshot.attention or "없음", inline=False)
        embed.add_field(name="다음 액션", value=snapshot.next_action or "없음", inline=False)
        embed.set_footer(text="Opscure live status")
        return embed

        embed = discord.Embed(
            title="Opscure 상태",
            description=(
                f"세션 `{snapshot.session_title}`\n"
                f"대상 `{snapshot.target_project_name}`\n"
                f"프로필 `{snapshot.profile_name}`"
            ),
            color=cls._status_color(snapshot),
            timestamp=utcnow(),
        )
        embed.add_field(
            name="현재 상태",
            value=(
                f"상태: `{snapshot.status}`\n"
                f"희망 상태: `{snapshot.desired_status}`\n"
                f"실행기: `{snapshot.launcher_id}`\n"
                f"작업 디렉터리: `{snapshot.workdir}`"
            ),
            inline=False,
        )
        if snapshot.active_worker_lines:
            workers_value = "\n".join(snapshot.active_worker_lines)
            if snapshot.activity_report_line:
                workers_value = f"{workers_value}\n{snapshot.activity_report_line}"
        else:
            workers_value = "대기 중\n현재 활성 작업 없음"
        embed.add_field(name="활성 작업자", value=workers_value, inline=False)
        embed.add_field(
            name="큐와 검증",
            value=(
                f"작업자 연결: {snapshot.worker_summary}\n"
                f"큐: {snapshot.job_summary}\n"
                f"검증: {snapshot.review_summary}"
            ),
            inline=False,
        )
        embed.add_field(
            name="주의",
            value=snapshot.attention or "없음",
            inline=False,
        )
        embed.add_field(
            name="다음 액션",
            value=snapshot.next_action or "없음",
            inline=False,
        )
        embed.set_footer(text="Opscure live status")
        return embed
