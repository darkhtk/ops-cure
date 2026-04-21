from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from .config_loader import ArtifactConfig
except ImportError:  # pragma: no cover - script mode support
    from config_loader import ArtifactConfig

REPORT_BLOCK_RE = re.compile(r"\[\[report\]\]\s*(?P<body>.*?)\s*\[\[/report\]\]", re.IGNORECASE | re.DOTALL)
ANSWER_BLOCK_RE = re.compile(r"\[\[answer\]\]\s*(?P<body>.*?)\s*\[\[/answer\]\]", re.IGNORECASE | re.DOTALL)
QUESTION_BLOCK_RE = re.compile(r"\[\[question\]\]\s*(?P<body>.*?)\s*\[\[/question\]\]", re.IGNORECASE | re.DOTALL)
DISCUSS_BLOCK_RE = re.compile(
    r"\[\[discuss(?P<attrs>[^\]]*)\]\]\s*(?P<body>.*?)\s*\[\[/discuss\]\]",
    re.IGNORECASE | re.DOTALL,
)
HANDOFF_BLOCK_RE = re.compile(
    r"\[\[handoff\s+agent=(?P<quote>['\"]?)(?P<agent>[A-Za-z0-9_-]+)(?P=quote)\s*\]\]\s*"
    r"(?P<body>.*?)\s*\[\[/handoff\]\]",
    re.IGNORECASE | re.DOTALL,
)
CONTROL_BLOCKS_RE = re.compile(
    r"\[\[(?:report|answer|question|discuss)(?:[^\]]*)\]\].*?\[\[/(?:report|answer|question|discuss)\]\]",
    re.IGNORECASE | re.DOTALL,
)
TASK_ID_RE = re.compile(r"\b(T-\d{3})\b", re.IGNORECASE)
TASK_STATUS_ORDER = [
    "ready",
    "in_progress",
    "verify",
    "review",
    "blocked_on_operator",
    "handoff_queued",
    "done",
    "failed",
]
ACTIVE_CHILD_TASK_STATUSES = {"ready", "in_progress", "verify", "review", "handoff_queued"}
PENDING_HANDOFF_TASK_STATUSES = {"ready", "handoff_queued"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_slug() -> str:
    return utcnow().strftime("%Y%m%d_%H%M%S")


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "session"


def trim_text(text: str, limit: int = 220) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def trim_thread_text(text: str, limit: int = 220) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    marker = " [truncated]"
    if limit <= len(marker):
        return normalized[:limit]
    return normalized[: limit - len(marker)].rstrip() + marker


@dataclass(slots=True)
class HandoffSpec:
    target_agent: str
    body: str
    task_id: str | None = None
    title: str | None = None


@dataclass(slots=True)
class DiscussSpec:
    discuss_type: str
    body: str
    task_id: str | None = None
    anomaly_id: str | None = None
    ask_agents: list[str] | None = None
    to_agent: str | None = None


@dataclass(slots=True)
class ParsedCliOutput:
    raw_output: str
    report_blocks: list[str]
    answer_blocks: list[str]
    question_blocks: list[str]
    discuss_blocks: list[DiscussSpec]
    handoffs: list[HandoffSpec]
    fallback_summary: str


@dataclass(slots=True)
class BridgeCompletionPayload:
    control_text: str
    thread_text: str


@dataclass(slots=True)
class TaskCard:
    task_id: str
    title: str
    owner: str
    status: str
    source_agent: str
    depends_on: str | None
    created_at: str
    updated_at: str
    latest_brief_name: str | None
    source_log_name: str | None
    goal: str
    definition_of_done: str
    notes: str


@dataclass(slots=True)
class SessionWorkspace:
    root: Path
    relative_root: Path
    session_name: str
    session_id: str
    project_workdir: Path
    agent_names: list[str]
    quiet_discord: bool

    @classmethod
    def create(
        cls,
        *,
        project_workdir: str | Path,
        artifacts: ArtifactConfig,
        session_name: str,
        session_id: str,
        agent_names: list[str],
    ) -> "SessionWorkspace":
        workdir = Path(project_workdir).resolve()
        sessions_root = (workdir / artifacts.sessions_dir).resolve()
        root_name = f"{slugify(session_name)}__{session_id[:8]}"
        root = sessions_root / root_name
        return cls(
            root=root,
            relative_root=Path(artifacts.sessions_dir) / root_name,
            session_name=session_name,
            session_id=session_id,
            project_workdir=workdir,
            agent_names=sorted(agent_names),
            quiet_discord=artifacts.quiet_discord,
        )

    @property
    def protocol_file(self) -> Path:
        return self.root / "SESSION_PROTOCOL.md"

    @property
    def status_file(self) -> Path:
        return self.root / "STATUS.md"

    @property
    def report_file(self) -> Path:
        return self.root / "REPORT.md"

    @property
    def state_file(self) -> Path:
        return self.root / "CURRENT_STATE.md"

    @property
    def questions_file(self) -> Path:
        return self.root / "CRITICAL_QUESTIONS.md"

    @property
    def handoffs_file(self) -> Path:
        return self.root / "HANDOFFS.md"

    @property
    def current_task_file(self) -> Path:
        return self.root / "CURRENT_TASK.md"

    @property
    def task_board_file(self) -> Path:
        return self.root / "TASK_BOARD.md"

    @property
    def task_index_file(self) -> Path:
        return self.root / ".task_index.json"

    @property
    def agents_dir(self) -> Path:
        return self.root / "AGENTS"

    @property
    def briefs_dir(self) -> Path:
        return self.root / "BRIEFS"

    @property
    def logs_dir(self) -> Path:
        return self.root / "RUN_LOGS"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "TASKS"

    def agent_file(self, agent_name: str) -> Path:
        return self.agents_dir / f"{agent_name}.md"

    def task_file(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id.upper()}.md"

    def ensure_structure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.briefs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        self.protocol_file.write_text(self._build_protocol_text(), encoding="utf-8")
        self._write_if_missing(self.state_file, self._build_state_template())
        self._write_if_missing(self.status_file, self._build_status_template())
        self._write_if_missing(self.report_file, self._build_report_template())
        self._write_if_missing(self.questions_file, self._build_questions_template())
        self._write_if_missing(self.handoffs_file, self._build_handoffs_template())
        self._write_if_missing(self.current_task_file, self._build_current_task_template())
        self._write_if_missing(self.task_board_file, self._build_task_board_template())
        self._write_if_missing(self.task_index_file, "{}\n")
        for agent_name in self.agent_names:
            self._write_if_missing(self.agent_file(agent_name), self._build_agent_template(agent_name))

    def build_heartbeat_snapshot(self) -> dict[str, object]:
        self.ensure_structure()
        state_text = self.state_file.read_text(encoding="utf-8") if self.state_file.exists() else ""
        current_task_text = self.current_task_file.read_text(encoding="utf-8") if self.current_task_file.exists() else ""
        tracked_files = [
            self.state_file,
            self.status_file,
            self.report_file,
            self.current_task_file,
            self.task_board_file,
            self.handoffs_file,
            self.task_index_file,
        ]
        latest_path = max(
            (path for path in tracked_files if path.exists()),
            key=lambda path: path.stat().st_mtime,
            default=None,
        )
        latest_artifact_at = (
            datetime.fromtimestamp(latest_path.stat().st_mtime, tz=timezone.utc).isoformat()
            if latest_path is not None
            else None
        )
        return {
            "workspace_ready": self.root.exists(),
            "state_label": self._extract_backticked_field(state_text, "Status"),
            "state_updated_at": self._extract_backticked_field(state_text, "Updated at"),
            "current_task_state": self._extract_backticked_field(current_task_text, "Session state"),
            "current_task_id": self._extract_backticked_field(current_task_text, "Task ID"),
            "current_task_updated_at": self._extract_backticked_field(current_task_text, "Updated at"),
            "latest_artifact_at": latest_artifact_at,
            "latest_artifact_path": self._display_path(latest_path) if latest_path is not None else None,
        }

    def reconcile_from_bridge_summary(self, summary: dict[str, object], *, agent_name: str) -> bool:
        self.ensure_structure()
        session_status = str(summary.get("status") or "unknown")
        desired_status = str(summary.get("desired_status") or "ready")
        pending_jobs = int(summary.get("pending_jobs") or 0)
        active_jobs = int(summary.get("active_jobs") or 0)
        agent_rows = [agent for agent in (summary.get("agents") or []) if isinstance(agent, dict)]
        summary_tasks = [task for task in (summary.get("tasks") or []) if isinstance(task, dict)]
        summary_handoffs = [handoff for handoff in (summary.get("queued_handoffs") or []) if isinstance(handoff, dict)]
        busy_agents = [
            str(agent.get("agent_name") or "").strip()
            for agent in agent_rows
            if str(agent.get("status") or "").strip().lower() == "busy"
        ]
        settled = (
            session_status in {"ready", "paused", "closed", "failed_start"}
            and pending_jobs == 0
            and active_jobs == 0
            and not busy_agents
        )
        index = self._task_index_from_bridge_summary(summary_tasks)
        if summary_tasks or summary_handoffs:
            self._save_task_index(index)
            self._remove_stale_task_files(index)
            for card in index.values():
                self._write_task_card(card)
        else:
            index = self._load_task_index()
        updated_files: list[Path] = []
        changed = bool(summary_tasks or summary_handoffs)

        if settled:
            if not summary_tasks and index:
                transient_statuses = {"in_progress", "review", "handoff_queued"}
                for card in index.values():
                    if card.status not in transient_statuses:
                        continue
                    card.status = "ready" if session_status in {"ready", "paused"} else "failed"
                    card.updated_at = utcnow().isoformat()
                    card.notes = (
                        "Reconciled from bridge session state after the session settled without active jobs."
                    )
                    self._write_task_card(card)
                    updated_files.append(self.task_file(card.task_id))
                    changed = True
            handoff_specs = self._handoff_specs_from_bridge_summary(summary_handoffs, index) if summary_handoffs else self._pending_handoffs_from_task_index(index)
            if handoff_specs:
                self.handoffs_file.write_text(
                    self._build_handoffs_text(agent_name=agent_name, handoffs=handoff_specs),
                    encoding="utf-8",
                )
            else:
                self.handoffs_file.write_text(self._build_handoffs_template(), encoding="utf-8")
            updated_files.append(self.handoffs_file)

            if changed:
                self._save_task_index(index)
            updated_files.append(self._refresh_task_board(index, reconcile=not (summary_tasks or summary_handoffs)))

            status_label = "paused" if desired_status == "paused" or session_status == "paused" else session_status
            blocker_text = str(summary.get("pause_reason") or "none") if status_label == "paused" else "none"
            next_action = self._bridge_next_action(
                session_status=session_status,
                desired_status=desired_status,
                attached_workers=sum(1 for agent in agent_rows if agent.get("worker_id")),
                total_agents=len(agent_rows),
            )
            summary_text = self._bridge_summary_text(
                session_status=session_status,
                pending_jobs=pending_jobs,
                active_jobs=active_jobs,
            )

            self.state_file.write_text(
                self._build_state_text(
                    status_label=status_label,
                    agent_name=agent_name,
                    summary=summary_text,
                    next_action=next_action,
                    blocker_text=blocker_text,
                    updated_files=updated_files or [self.state_file],
                ),
                encoding="utf-8",
            )
            self.current_task_file.write_text(
                self._build_current_task_summary(
                    agent_name=agent_name,
                    state_label=status_label,
                    next_action=next_action,
                    latest_brief_name=None,
                    task_id=None,
                    task_path=None,
                    job_type="status_sync",
                    current_message="No active job. Local artifacts were synchronized from the bridge session summary.",
                ),
                encoding="utf-8",
            )
            self.status_file.write_text(
                self._build_status_text(
                    agent_name=agent_name,
                    report_text=summary_text,
                    questions=[],
                    handoff_lines=self._extract_handoff_lines(handoff_specs) if handoff_specs else ["none"],
                    updated_files=updated_files or [self.state_file, self.current_task_file],
                ),
                encoding="utf-8",
            )
            return True

        active_agent_name = busy_agents[0] if busy_agents else agent_name
        active_task_id = next(
            (
                str(task.get("task_key") or "").strip().upper()
                for task in summary_tasks
                if str(task.get("state") or "").strip().lower() == "in_progress"
            ),
            None,
        )
        if not active_task_id:
            active_task_id = self._resolve_active_task_id(index=index, active_agent_name=active_agent_name)
        active_task_path: Path | None = None
        if active_task_id and not summary_tasks:
            card = index.get(active_task_id) or TaskCard(
                task_id=active_task_id,
                title=f"Active task {active_task_id}",
                owner=active_agent_name,
                status="in_progress",
                source_agent=active_agent_name,
                depends_on=None,
                created_at=utcnow().isoformat(),
                updated_at=utcnow().isoformat(),
                latest_brief_name=None,
                source_log_name=None,
                goal="Continue the active task described by the bridge session summary.",
                definition_of_done="Finish the active task and update the local session artifacts.",
                notes="Synthesized from bridge session summary.",
            )
            card.owner = active_agent_name
            card.status = "in_progress"
            card.updated_at = utcnow().isoformat()
            index[active_task_id] = card
            self._write_task_card(card)
            active_task_path = self.task_file(active_task_id)
            updated_files.append(active_task_path)
            changed = True
        elif active_task_id:
            active_task_path = self.task_file(active_task_id)
            if active_task_path.exists():
                updated_files.append(active_task_path)

        pending_handoffs = (
            self._handoff_specs_from_bridge_summary(summary_handoffs, index, exclude_task_id=active_task_id)
            if summary_handoffs
            else self._pending_handoffs_from_task_index(index, exclude_task_id=active_task_id)
        )
        if pending_handoffs:
            self.handoffs_file.write_text(
                self._build_handoffs_text(agent_name=active_agent_name, handoffs=pending_handoffs),
                encoding="utf-8",
            )
        else:
            self.handoffs_file.write_text(self._build_handoffs_template(), encoding="utf-8")
        updated_files.append(self.handoffs_file)

        if changed:
            self._save_task_index(index)
        updated_files.append(self._refresh_task_board(index, reconcile=not (summary_tasks or summary_handoffs)))

        next_action = self._bridge_next_action(
            session_status=session_status,
            desired_status=desired_status,
            attached_workers=sum(1 for agent in agent_rows if agent.get("worker_id")),
            total_agents=len(agent_rows),
            active_agent_name=active_agent_name,
        )
        summary_text = self._bridge_summary_text(
            session_status=session_status,
            pending_jobs=pending_jobs,
            active_jobs=active_jobs,
            active_agent_name=active_agent_name,
            active_task_id=active_task_id,
        )

        self.state_file.write_text(
            self._build_state_text(
                status_label="in_progress",
                agent_name=active_agent_name,
                summary=summary_text,
                next_action=next_action,
                blocker_text="none",
                updated_files=updated_files or [self.state_file],
            ),
            encoding="utf-8",
        )
        self.current_task_file.write_text(
            self._build_current_task_summary(
                agent_name=active_agent_name,
                state_label="in_progress",
                next_action=next_action,
                latest_brief_name=None,
                task_id=active_task_id,
                task_path=active_task_path,
                job_type="status_sync",
                current_message=(
                    f"Bridge session summary says `{active_agent_name}` is working on "
                    f"`{active_task_id or 'the active task'}`. Read `TASKS/{active_task_id}.md` and `CURRENT_STATE.md` first."
                    if active_task_id
                    else f"Bridge session summary says `{active_agent_name}` is processing active work."
                ),
            ),
            encoding="utf-8",
        )
        self.status_file.write_text(
            self._build_status_text(
                agent_name=active_agent_name,
                report_text=summary_text,
                questions=[],
                handoff_lines=self._extract_handoff_lines(pending_handoffs) if pending_handoffs else ["none"],
                updated_files=updated_files or [self.state_file, self.current_task_file],
            ),
            encoding="utf-8",
        )
        return True

    def write_job_brief(
        self,
        *,
        agent_name: str,
        job_type: str,
        user_text: str,
        session_summary: str | None,
        recent_transcript: list[dict[str, object]],
        task_id: str | None = None,
    ) -> Path:
        self.ensure_structure()
        stamp = timestamp_slug()
        brief_path = self.briefs_dir / f"{stamp}_{agent_name}.md"
        transcript_lines = [
            f"- [{entry.get('direction', 'unknown')}] {entry.get('actor', 'unknown')}: {entry.get('content', '')}"
            for entry in recent_transcript
        ]
        transcript_text = "\n".join(transcript_lines) if transcript_lines else "- none"
        brief_path.write_text(
            (
                f"# Job Brief\n\n"
                f"- Session: `{self.session_name}` (`{self.session_id}`)\n"
                f"- Agent: `{agent_name}`\n"
                f"- Job type: `{job_type}`\n"
                f"- Timestamp: `{utcnow().isoformat()}`\n\n"
                f"## Current message\n\n{user_text.strip() or '(empty message)'}\n\n"
                f"## Session memory\n\n{(session_summary or '- none').strip()}\n\n"
                f"## Recent transcript\n\n{transcript_text}\n"
            ),
            encoding="utf-8",
        )

        resolved_task_id = task_id or self._extract_task_id(user_text)
        if resolved_task_id is None and job_type == "orchestration":
            resolved_task_id = self._allocate_task_id(self._load_task_index())
        task_path: Path | None = None
        if resolved_task_id:
            task_path = self._claim_task_for_job(
                task_id=resolved_task_id,
                agent_name=agent_name,
                user_text=user_text,
                brief_name=brief_path.name,
            )

        self.current_task_file.write_text(
            self._build_current_task_summary(
                agent_name=agent_name,
                state_label="in_progress",
                next_action="Work the current brief and update markdown artifacts before responding.",
                latest_brief_name=brief_path.name,
                task_id=resolved_task_id,
                task_path=task_path,
                job_type=job_type,
                current_message=user_text,
            ),
            encoding="utf-8",
        )
        return brief_path

    def record_cli_result(
        self,
        *,
        agent_name: str,
        job_type: str,
        user_text: str,
        raw_output: str,
        task_id: str | None = None,
        preserve_handoffs: bool = True,
    ) -> BridgeCompletionPayload:
        self.ensure_structure()
        parsed = self._parse_cli_output(raw_output)
        stamp = timestamp_slug()
        raw_log_path = self.logs_dir / f"{stamp}_{agent_name}.md"
        raw_log_path.write_text(parsed.raw_output or "(no output)", encoding="utf-8")

        current_task_id = task_id or self._extract_task_id(user_text)
        updated_files: list[Path] = [raw_log_path]
        answer_text = parsed.answer_blocks[-1] if parsed.answer_blocks else None
        discuss_text = parsed.discuss_blocks[-1].body if parsed.discuss_blocks else None
        report_text = (
            parsed.report_blocks[-1]
            if parsed.report_blocks
            else answer_text or discuss_text or parsed.fallback_summary
        )

        handoffs, handoff_files = self._materialize_handoffs(
            source_agent=agent_name,
            handoffs=parsed.handoffs,
            parent_task_id=current_task_id,
            source_log_name=raw_log_path.name,
        )
        updated_files.extend(handoff_files)

        if report_text:
            self.report_file.write_text(
                (
                    f"# Report To User\n\n"
                    f"- Session: `{self.session_name}`\n"
                    f"- Last updated by: `{agent_name}`\n"
                    f"- Updated at: `{utcnow().isoformat()}`\n"
                    f"- Raw log: `{raw_log_path.name}`\n\n"
                    f"{report_text}\n"
                ),
                encoding="utf-8",
            )
            updated_files.append(self.report_file)

        self.agent_file(agent_name).write_text(
            (
                f"# Agent Notes: {agent_name}\n\n"
                f"- Session: `{self.session_name}`\n"
                f"- Last updated: `{utcnow().isoformat()}`\n"
                f"- Raw log: `{raw_log_path.name}`\n\n"
                f"## Latest report\n\n{report_text or '(none)'}\n"
            ),
            encoding="utf-8",
        )
        updated_files.append(self.agent_file(agent_name))

        if parsed.question_blocks:
            sections = [f"## Question {index}\n\n{question}" for index, question in enumerate(parsed.question_blocks, start=1)]
            self.questions_file.write_text(
                (
                    f"# Critical Questions\n\n"
                    f"- Session: `{self.session_name}`\n"
                    f"- Asked by: `{agent_name}`\n"
                    f"- Updated at: `{utcnow().isoformat()}`\n"
                    f"- Raw log: `{raw_log_path.name}`\n\n"
                    f"{chr(10).join(sections)}\n"
                ),
                encoding="utf-8",
            )
        else:
            self.questions_file.write_text(self._build_questions_template(), encoding="utf-8")
        updated_files.append(self.questions_file)

        if handoffs:
            self.handoffs_file.write_text(self._build_handoffs_text(agent_name=agent_name, handoffs=handoffs), encoding="utf-8")
        else:
            self.handoffs_file.write_text(self._build_handoffs_template(), encoding="utf-8")
        updated_files.append(self.handoffs_file)

        task_files = self._finalize_current_task(
            task_id=current_task_id,
            agent_name=agent_name,
            report_text=report_text,
            question_blocks=parsed.question_blocks,
            handoffs=handoffs,
            raw_log_name=raw_log_path.name,
        )
        updated_files.extend(task_files)

        if parsed.question_blocks:
            state_label = "blocked_on_operator"
            blocker_text = parsed.question_blocks[0]
            next_action = "Wait for the operator to answer the blocking question."
        elif handoffs:
            state_label = "handoff_queued"
            blocker_text = "none"
            next_action = (
                "Continue through `TASK_BOARD.md`, `TASKS/*.md`, `HANDOFFS.md`, and the relevant "
                "agent notes."
            )
        else:
            state_label = "idle"
            blocker_text = "none"
            next_action = "Waiting for the next user message or job."

        self.state_file.write_text(
            self._build_state_text(
                status_label=state_label,
                agent_name=agent_name,
                summary=report_text or "(none)",
                next_action=next_action,
                blocker_text=blocker_text,
                updated_files=updated_files,
            ),
            encoding="utf-8",
        )
        updated_files.append(self.state_file)

        self.current_task_file.write_text(
            self._build_current_task_summary(
                agent_name=agent_name,
                state_label=state_label,
                next_action=next_action,
                latest_brief_name=None,
                task_id=current_task_id,
                task_path=self.task_file(current_task_id) if current_task_id else None,
                job_type=job_type,
                current_message=user_text,
            ),
            encoding="utf-8",
        )
        updated_files.append(self.current_task_file)

        self.status_file.write_text(
            self._build_status_text(
                agent_name=agent_name,
                report_text=report_text,
                questions=parsed.question_blocks,
                handoff_lines=self._extract_handoff_lines(handoffs),
                updated_files=updated_files,
            ),
            encoding="utf-8",
        )
        updated_files.append(self.status_file)

        return BridgeCompletionPayload(
            control_text=self._compose_control_output(
                parsed=parsed,
                preserve_handoffs=preserve_handoffs,
            ),
            thread_text=self._compose_thread_output(
                agent_name=agent_name,
                task_id=current_task_id,
                report_text=report_text,
                answer_text=answer_text,
                question_blocks=parsed.question_blocks,
                discuss_blocks=parsed.discuss_blocks,
                handoffs=handoffs if preserve_handoffs else [],
                updated_files=updated_files,
            ),
        )

    def record_cli_failure(
        self,
        *,
        agent_name: str,
        job_type: str,
        user_text: str,
        summary: str,
        stdout_text: str = "",
        stderr_text: str = "",
        task_id: str | None = None,
        planner_recovery_expected: bool = False,
    ) -> str:
        self.ensure_structure()
        stamp = timestamp_slug()
        raw_log_path = self.logs_dir / f"{stamp}_{agent_name}_error.md"
        raw_log_path.write_text(
            (
                f"# CLI Failure\n\n"
                f"- Session: `{self.session_name}`\n"
                f"- Agent: `{agent_name}`\n"
                f"- Logged at: `{utcnow().isoformat()}`\n\n"
                f"- Job type: `{job_type}`\n\n"
                f"## Summary\n\n{summary}\n\n"
                f"## stdout\n\n{stdout_text.strip() or '(empty)'}\n\n"
                f"## stderr\n\n{stderr_text.strip() or '(empty)'}\n"
            ),
            encoding="utf-8",
        )

        self.report_file.write_text(
            (
                f"# Report To User\n\n"
                f"- Session: `{self.session_name}`\n"
                f"- Last updated by: `{agent_name}`\n"
                f"- Updated at: `{utcnow().isoformat()}`\n"
                f"- Failure log: `{raw_log_path.name}`\n\n"
                f"{summary}\n"
            ),
            encoding="utf-8",
        )

        self.agent_file(agent_name).write_text(
            (
                f"# Agent Notes: {agent_name}\n\n"
                f"- Session: `{self.session_name}`\n"
                f"- Last updated: `{utcnow().isoformat()}`\n"
                f"- Failure log: `{raw_log_path.name}`\n\n"
                f"## Latest failure\n\n{summary}\n"
            ),
            encoding="utf-8",
        )

        self.questions_file.write_text(self._build_questions_template(), encoding="utf-8")
        current_task_id = task_id or self._extract_task_id(user_text)
        task_files = self._mark_task_failed(
            task_id=current_task_id,
            agent_name=agent_name,
            summary=summary,
            raw_log_name=raw_log_path.name,
        )
        self.handoffs_file.write_text(self._build_handoffs_template(), encoding="utf-8")

        state_label = "needs_recovery" if planner_recovery_expected else "failed_waiting_for_triage"
        next_action = (
            "Planner recovery should summarize the failure, shrink the next task, and queue the next step."
            if planner_recovery_expected
            else "A human or planner follow-up must decide whether to retry, split, or stop."
        )
        updated_files = [
            raw_log_path,
            self.report_file,
            self.agent_file(agent_name),
            self.questions_file,
            self.handoffs_file,
            *task_files,
        ]
        self.state_file.write_text(
            self._build_state_text(
                status_label=state_label,
                agent_name=agent_name,
                summary=summary,
                next_action=next_action,
                blocker_text=summary,
                updated_files=updated_files,
            ),
            encoding="utf-8",
        )
        updated_files.append(self.state_file)

        self.current_task_file.write_text(
            self._build_current_task_summary(
                agent_name=agent_name,
                state_label=state_label,
                next_action=next_action,
                latest_brief_name=None,
                task_id=current_task_id,
                task_path=self.task_file(current_task_id) if current_task_id else None,
                job_type=job_type,
                current_message=user_text,
            ),
            encoding="utf-8",
        )
        updated_files.append(self.current_task_file)

        self.status_file.write_text(
            self._build_status_text(
                agent_name=agent_name,
                report_text=summary,
                questions=[],
                handoff_lines=["planner recovery expected" if planner_recovery_expected else "none"],
                updated_files=updated_files,
            ),
            encoding="utf-8",
        )

        read_pointer = self._compose_read_pointer(
            task_id=current_task_id,
            extra_paths=[raw_log_path, self.state_file, self.status_file],
        )
        human_text = self._compose_human_summary(summary)
        issue_text = "planner_recovery_expected" if planner_recovery_expected else "triage_required"
        return "\n".join(
            [
                (
                    f"OPS: type=failed | actor={agent_name} | task={current_task_id or 'none'} | "
                    f"state=failed | read={read_pointer} | reason=cli_failure"
                ),
                f"HUMAN: {human_text}",
                f"ISSUE: {issue_text}",
            ],
        )

    def _parse_cli_output(self, raw_output: str) -> ParsedCliOutput:
        normalized = raw_output.strip()
        reports = [match.group("body").strip() for match in REPORT_BLOCK_RE.finditer(normalized) if match.group("body").strip()]
        answers = [match.group("body").strip() for match in ANSWER_BLOCK_RE.finditer(normalized) if match.group("body").strip()]
        questions = [match.group("body").strip() for match in QUESTION_BLOCK_RE.finditer(normalized) if match.group("body").strip()]
        discusses: list[DiscussSpec] = []
        for match in DISCUSS_BLOCK_RE.finditer(normalized):
            body = match.group("body").strip()
            if not body:
                continue
            attrs = self._parse_discuss_attrs(match.group("attrs") or "")
            discuss_type = (attrs.get("type") or "open").strip().lower()
            ask_raw = (attrs.get("ask") or attrs.get("with") or "").strip()
            ask_agents = [item.strip() for item in ask_raw.split(",") if item.strip()] or None
            to_agent = (attrs.get("to") or "").strip() or None
            anomaly_id = (attrs.get("anomaly") or attrs.get("id") or "").strip() or None
            discusses.append(
                DiscussSpec(
                    discuss_type=discuss_type,
                    body=body,
                    task_id=self._extract_task_id(body),
                    anomaly_id=anomaly_id,
                    ask_agents=ask_agents,
                    to_agent=to_agent,
                ),
            )
        handoffs: list[HandoffSpec] = []
        for match in HANDOFF_BLOCK_RE.finditer(normalized):
            body = match.group("body").strip()
            if not body:
                continue
            handoffs.append(
                HandoffSpec(
                    target_agent=match.group("agent").strip(),
                    body=body,
                    task_id=self._extract_task_id(body),
                    title=self._derive_task_title(body),
                ),
            )
        without_controls = CONTROL_BLOCKS_RE.sub("", normalized)
        without_handoffs = HANDOFF_BLOCK_RE.sub("", without_controls)
        without_discuss = DISCUSS_BLOCK_RE.sub("", without_handoffs)
        visible_fallback = without_discuss.strip()
        if not visible_fallback and answers:
            visible_fallback = answers[-1]
        elif not visible_fallback and discusses:
            visible_fallback = discusses[-1].body
        elif not visible_fallback and reports:
            visible_fallback = reports[-1]
        fallback = trim_text(visible_fallback or normalized or "(no output)")
        return ParsedCliOutput(
            raw_output=normalized,
            report_blocks=reports,
            answer_blocks=answers,
            question_blocks=questions,
            discuss_blocks=discusses,
            handoffs=handoffs,
            fallback_summary=fallback,
        )

    def _compose_control_output(
        self,
        *,
        parsed: ParsedCliOutput,
        preserve_handoffs: bool,
    ) -> str:
        if preserve_handoffs:
            return parsed.raw_output
        return HANDOFF_BLOCK_RE.sub("", parsed.raw_output).strip()

    def _compose_thread_output(
        self,
        *,
        agent_name: str,
        task_id: str | None,
        report_text: str,
        answer_text: str | None,
        question_blocks: list[str],
        discuss_blocks: list[DiscussSpec],
        handoffs: list[HandoffSpec],
        updated_files: list[Path],
    ) -> str:
        del updated_files
        human_text = self._compose_human_summary(report_text)
        answer_summary = self._compose_answer_summary(answer_text)
        discuss_lines = self._compose_discuss_lines(
            agent_name=agent_name,
            task_id=task_id,
            discuss_blocks=discuss_blocks,
        )
        if question_blocks:
            read_pointer = self._compose_read_pointer(
                task_id=task_id,
                extra_paths=[self.questions_file, self.state_file],
            )
            issue_text = trim_thread_text(" ".join(question_blocks[0].split()), 220)
            return "\n".join(
                [
                    (
                        f"OPS: type=blocked | actor={agent_name} | task={task_id or 'none'} | "
                        f"state=blocked | read={read_pointer} | reason=operator_input_required"
                    ),
                    f"HUMAN: {human_text}",
                    f"ISSUE: {issue_text}",
                ],
            )

        if discuss_lines:
            lines = [*discuss_lines]
            if answer_summary:
                lines.append(f"ANSWER: {answer_summary}")
            lines.append(f"HUMAN: {human_text}")
            return "\n".join(lines)

        if handoffs:
            ops_lines = [
                (
                    f"OPS: type=handoff | task={handoff.task_id or 'none'} | from={agent_name} | "
                    f"to={handoff.target_agent} | state=ready | "
                    f"read={self._compose_read_pointer(task_id=handoff.task_id, extra_paths=[self.state_file])}"
                )
                for handoff in handoffs
            ]
            lines = [*ops_lines]
            if answer_summary:
                lines.append(f"ANSWER: {answer_summary}")
            lines.append(f"HUMAN: {human_text}")
            return "\n".join(lines)

        if answer_summary:
            read_pointer = self._compose_read_pointer(task_id=task_id, extra_paths=[self.state_file, self.report_file])
            return "\n".join(
                [
                    (
                        f"OPS: type=answer | actor={agent_name} | task={task_id or 'none'} | "
                        f"state=answered | read={read_pointer}"
                    ),
                    f"ANSWER: {answer_summary}",
                    f"HUMAN: {human_text}",
                ],
            )

        read_pointer = self._compose_read_pointer(task_id=task_id, extra_paths=[self.state_file, self.report_file])
        done_line = f"DONE: task={task_id}" if task_id else "DONE: state=idle"
        return "\n".join(
            [
                (
                    f"OPS: type=done | actor={agent_name} | task={task_id or 'none'} | "
                    f"state=idle | read={read_pointer}"
                ),
                f"HUMAN: {human_text}",
                done_line,
            ],
        )

    def _compose_discuss_lines(
        self,
        *,
        agent_name: str,
        task_id: str | None,
        discuss_blocks: list[DiscussSpec],
    ) -> list[str]:
        lines: list[str] = []
        for discuss in discuss_blocks:
            effective_task_id = discuss.task_id or task_id
            read_pointer = self._compose_read_pointer(
                task_id=effective_task_id,
                extra_paths=[self.state_file, self.task_file(effective_task_id)] if effective_task_id else [self.state_file],
            )
            anomaly = discuss.anomaly_id or "none"
            if discuss.discuss_type == "open":
                ask = ",".join(discuss.ask_agents or []) or "none"
                lines.append(
                    f"OPS: type=discuss_open | anomaly={anomaly} | actor={agent_name} | ask={ask} | read={read_pointer}"
                )
            elif discuss.discuss_type == "reply":
                target = discuss.to_agent or "none"
                lines.append(
                    f"OPS: type=discuss_reply | anomaly={anomaly} | actor={agent_name} | to={target} | read={read_pointer}"
                )
            elif discuss.discuss_type == "resolve":
                lines.append(
                    f"OPS: type=discuss_resolve | anomaly={anomaly} | actor={agent_name} | read={read_pointer}"
                )
            elif discuss.discuss_type == "escalate":
                lines.append(
                    f"OPS: type=discuss_escalate | anomaly={anomaly} | actor={agent_name} | read={read_pointer}"
                )
        return lines

    def _compose_human_summary(self, report_text: str) -> str:
        return " ".join((report_text or "(no update)").split()) or "(no update)"

    def _compose_answer_summary(self, answer_text: str | None) -> str | None:
        if not answer_text:
            return None
        normalized = " ".join(answer_text.split()).strip()
        if not normalized:
            return None
        limit = 420 if not self.quiet_discord else 220
        return trim_thread_text(normalized, limit)

    def _parse_discuss_attrs(self, attrs_text: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(['\"])(.*?)\2", attrs_text):
            attrs[match.group(1).strip().lower()] = match.group(3).strip()
        return attrs

    def _compose_read_pointer(
        self,
        *,
        task_id: str | None,
        extra_paths: list[Path] | None = None,
    ) -> str:
        ordered: list[str] = []
        paths: list[Path] = [self.state_file]
        if task_id:
            paths.append(self.task_file(task_id))
        if extra_paths:
            paths.extend(extra_paths)
        for path in paths:
            display = self._display_path(path)
            if display not in ordered:
                ordered.append(display)
        return ",".join(ordered)

    def _pending_handoffs_from_task_index(
        self,
        index: dict[str, TaskCard],
        *,
        exclude_task_id: str | None = None,
    ) -> list[HandoffSpec]:
        handoffs: list[HandoffSpec] = []
        for card in sorted(index.values(), key=lambda item: item.task_id):
            if exclude_task_id and card.task_id == exclude_task_id:
                continue
            if card.status not in PENDING_HANDOFF_TASK_STATUSES:
                continue
            handoffs.append(
                HandoffSpec(
                    target_agent=card.owner,
                    body=card.goal or card.notes or card.title,
                    task_id=card.task_id,
                    title=card.title,
                ),
            )
        return handoffs

    def _handoff_specs_from_bridge_summary(
        self,
        handoffs: list[dict[str, object]],
        index: dict[str, TaskCard],
        *,
        exclude_task_id: str | None = None,
    ) -> list[HandoffSpec]:
        specs: list[HandoffSpec] = []
        for handoff in handoffs:
            task_id = str(handoff.get("task_key") or "").strip().upper() or None
            if exclude_task_id and task_id == exclude_task_id:
                continue
            target_agent = str(handoff.get("target_agent") or "").strip()
            body_text = str(handoff.get("body_text") or "").strip()
            title = index.get(task_id).title if task_id and task_id in index else None
            if not target_agent or not body_text:
                continue
            specs.append(
                HandoffSpec(
                    target_agent=target_agent,
                    body=body_text,
                    task_id=task_id,
                    title=title,
                ),
            )
        return specs

    def _task_index_from_bridge_summary(self, tasks: list[dict[str, object]]) -> dict[str, TaskCard]:
        index: dict[str, TaskCard] = {}
        for task in tasks:
            task_id = str(task.get("task_key") or "").strip().upper()
            if not task_id:
                continue
            body_text = str(task.get("body_text") or "").strip()
            summary_text = str(task.get("summary_text") or "").strip()
            index[task_id] = TaskCard(
                task_id=task_id,
                title=str(task.get("title") or task_id).strip() or task_id,
                owner=str(task.get("assigned_agent") or task.get("role") or "unassigned").strip() or "unassigned",
                status=str(task.get("state") or "ready").strip() or "ready",
                source_agent=str(task.get("source_agent") or "bridge").strip() or "bridge",
                depends_on=str(task.get("depends_on_task_key") or "").strip() or None,
                created_at=str(task.get("created_at") or utcnow().isoformat()),
                updated_at=str(task.get("updated_at") or utcnow().isoformat()),
                latest_brief_name=str(task.get("latest_brief_name") or "").strip() or None,
                source_log_name=str(task.get("latest_log_name") or "").strip() or None,
                goal=body_text or summary_text or "Continue the referenced task.",
                definition_of_done=summary_text or "Complete the task and update the local session artifacts.",
                notes=summary_text or body_text or "Rebuilt from bridge task summary.",
            )
        return index

    def _resolve_active_task_id(self, *, index: dict[str, TaskCard], active_agent_name: str) -> str | None:
        candidate_ids = [
            card.task_id
            for card in sorted(index.values(), key=lambda item: item.task_id)
            if card.owner == active_agent_name and card.status in {"ready", "handoff_queued", "in_progress", "review"}
        ]
        if len(candidate_ids) == 1:
            return candidate_ids[0]
        return None

    def _remove_stale_task_files(self, index: dict[str, TaskCard]) -> None:
        keep = {self.task_file(task_id).resolve() for task_id in index}
        for path in self.tasks_dir.glob("T-*.md"):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in keep:
                continue
            path.unlink(missing_ok=True)

    def _build_protocol_text(self) -> str:
        agent_mentions = ", ".join(f"`{name}`" for name in self.agent_names)
        return (
            "# Session Protocol\n\n"
            "This workspace is the source of truth for long-form collaboration artifacts.\n\n"
            "## Discord Is The Async Event Bus\n\n"
            "- Treat the shared thread as a compact async event bus for agent collaboration.\n"
            "- Use thread messages to announce ownership changes, next readers, blockers, and completion.\n"
            "- Keep detailed payloads, checklists, logs, and reasoning in local markdown files.\n"
            "- Thread-visible updates should stay short and point to the markdown files that matter.\n\n"
            "## Task-Card Workflow\n\n"
            f"- The agents in this session are: {agent_mentions}.\n"
            "- `TASK_BOARD.md` is the overview of ready, active, blocked, and done work.\n"
            "- `TASKS/T-###.md` files are the durable task cards.\n"
            "- Each handoff should describe one focused next action that can become a task card.\n"
            "- Independent tasks may be handed to multiple agents in parallel only when the file scope does not overlap.\n"
            "- `CURRENT_STATE.md` remains the single-source summary for the operator.\n\n"
            "## Required stdout blocks\n\n"
            "[[report]]\nOne short human-readable sentence.\n[[/report]]\n\n"
            "[[answer]]\nDirect answer to the operator's question.\n[[/answer]]\n\n"
            "[[question]]\nOnly if a critical blocking decision is needed.\n[[/question]]\n\n"
            "[[discuss type=\"open\" ask=\"reviewer,coder\" anomaly=\"A-001\"]]\n"
            "Short async discussion opener for anomalies, feature-intent misunderstandings, or review disagreements.\n"
            "[[/discuss]]\n\n"
            "[[handoff agent=\"coder\"]]\n"
            "T-002\n"
            "Target summary: One focused next action.\n"
            "Read CURRENT_STATE.md and TASK_BOARD.md first.\n"
            "Files: src/example.py\n"
            "Done condition: concrete finish state.\n"
            "[[/handoff]]\n\n"
            "- Every handoff body must include a `T-###` id, `Target summary:`, and the `Read CURRENT_STATE.md and TASK_BOARD.md first.` reminder or the bridge will reject it.\n\n"
            "## Thread-visible protocol\n\n"
            "- The runtime turns stdout plus handoff metadata into compact thread events.\n"
            "- Every thread event should contain at least one `OPS:` line and one `HUMAN:` line.\n"
            "- `OPS:` is the async event bus line for agents and the bridge. Keep it terse and structured.\n"
            "- `HUMAN:` is a short sentence for operator observability.\n"
            "- `ANSWER:` is used when an agent directly answers the operator's question.\n"
            "- Emit `[[answer]]...[[/answer]]` when the primary thread-visible output should be a direct answer.\n"
            "- Open a `[[discuss ...]]` block when a state anomaly, feature-intent misunderstanding, or review-interpretation mismatch should be clarified between agents before more work is queued.\n"
            "- Use `ISSUE:` only when blocked or failed, and `DONE:` when work completes without a handoff.\n\n"
            "## Key files\n\n"
            "- `CURRENT_STATE.md`: single-source summary of status, blocker, and next action.\n"
            "- `TASK_BOARD.md`: session-wide task queue snapshot.\n"
            "- `TASKS/*.md`: durable task cards with ownership and completion criteria.\n"
            "- `STATUS.md`: current state, most recent report, updated files.\n"
            "- `REPORT.md`: latest operator-facing report.\n"
            "- `CRITICAL_QUESTIONS.md`: unresolved blocking questions.\n"
            "- `HANDOFFS.md`: queued internal handoffs and task IDs.\n"
            "- `AGENTS/<agent>.md`: latest role-specific notes.\n"
            "- `BRIEFS/`: per-job local briefs.\n"
            "- `RUN_LOGS/`: raw CLI outputs archived by the runtime.\n"
        )

    def _build_state_template(self) -> str:
        return (
            "# Current State\n\n"
            f"- Session: `{self.session_name}`\n"
            f"- Session ID: `{self.session_id}`\n"
            f"- Workspace root: `{self.relative_root.as_posix()}`\n"
            "- Status: waiting_for_work\n"
            "- Last owner: none\n"
            "- Next action: wait for the next message\n"
            "- Blocker: none\n"
        )

    def _build_status_template(self) -> str:
        return (
            "# Status\n\n"
            f"- Session: `{self.session_name}`\n"
            f"- Session ID: `{self.session_id}`\n"
            f"- Workspace root: `{self.relative_root.as_posix()}`\n"
            f"- Project workdir: `{self.project_workdir}`\n"
            "- Current state: waiting for work\n"
        )

    def _build_report_template(self) -> str:
        return "# Report To User\n\nNo report has been written yet.\n"

    def _build_questions_template(self) -> str:
        return "# Critical Questions\n\nNo critical questions are currently recorded.\n"

    def _build_handoffs_template(self) -> str:
        return "# Handoffs\n\nNo internal handoffs are currently recorded.\n"

    def _build_current_task_template(self) -> str:
        return "# Current Task\n\nNo active task is recorded yet.\n"

    def _build_task_board_template(self) -> str:
        return (
            "# Task Board\n\n"
            f"- Session: `{self.session_name}`\n"
            f"- Session ID: `{self.session_id}`\n"
            f"- Workspace root: `{self.relative_root.as_posix()}`\n\n"
            "No task cards have been recorded yet.\n"
        )

    def _build_agent_template(self, agent_name: str) -> str:
        return f"# Agent Notes: {agent_name}\n\nNo notes have been written yet.\n"

    def _build_status_text(
        self,
        *,
        agent_name: str,
        report_text: str,
        questions: list[str],
        handoff_lines: list[str],
        updated_files: list[Path],
    ) -> str:
        file_lines = "\n".join(f"- `{self._display_path(path)}`" for path in updated_files)
        question_text = "\n".join(f"- {trim_text(item, 280)}" for item in questions) or "- none"
        handoff_text = "\n".join(f"- {line}" for line in handoff_lines) or "- none"
        return (
            "# Status\n\n"
            f"- Session: `{self.session_name}`\n"
            f"- Session ID: `{self.session_id}`\n"
            f"- Workspace root: `{self.relative_root.as_posix()}`\n"
            f"- Project workdir: `{self.project_workdir}`\n"
            f"- Last updated by: `{agent_name}`\n"
            f"- Updated at: `{utcnow().isoformat()}`\n\n"
            "## Latest report\n\n"
            f"{report_text or '(none)'}\n\n"
            "## Critical questions\n\n"
            f"{question_text}\n\n"
            "## Internal handoffs\n\n"
            f"{handoff_text}\n\n"
            "## Updated files\n\n"
            f"{file_lines}\n"
        )

    def _build_state_text(
        self,
        *,
        status_label: str,
        agent_name: str,
        summary: str,
        next_action: str,
        blocker_text: str,
        updated_files: list[Path],
    ) -> str:
        file_lines = "\n".join(f"- `{self._display_path(path)}`" for path in updated_files)
        return (
            "# Current State\n\n"
            f"- Session: `{self.session_name}`\n"
            f"- Session ID: `{self.session_id}`\n"
            f"- Workspace root: `{self.relative_root.as_posix()}`\n"
            f"- Status: `{status_label}`\n"
            f"- Last owner: `{agent_name}`\n"
            f"- Updated at: `{utcnow().isoformat()}`\n\n"
            "## Summary\n\n"
            f"{summary or '(none)'}\n\n"
            "## Next action\n\n"
            f"{next_action}\n\n"
            "## Blocker\n\n"
            f"{blocker_text}\n\n"
            "## Key files\n\n"
            f"{file_lines}\n"
        )

    def _build_handoffs_text(self, *, agent_name: str, handoffs: list[HandoffSpec]) -> str:
        summary_lines = "\n".join(
            f"- `{handoff.task_id or 'task-pending'}` -> `{handoff.target_agent}`: {trim_text(handoff.title or handoff.body, 180)}"
            for handoff in handoffs
        )
        detail_sections = "\n\n".join(
            (
                f"## {handoff.task_id or handoff.target_agent}\n\n"
                f"- Target agent: `{handoff.target_agent}`\n"
                f"- Title: {handoff.title or trim_text(handoff.body, 80)}\n\n"
                f"{handoff.body}"
            )
            for handoff in handoffs
        )
        return (
            "# Handoffs\n\n"
            f"- Session: `{self.session_name}`\n"
            f"- Source agent: `{agent_name}`\n"
            f"- Updated at: `{utcnow().isoformat()}`\n\n"
            "## Active summary\n\n"
            f"{summary_lines}\n\n"
            "## Details\n\n"
            f"{detail_sections}\n"
        )

    def _build_current_task_summary(
        self,
        *,
        agent_name: str,
        state_label: str,
        next_action: str,
        latest_brief_name: str | None,
        task_id: str | None,
        task_path: Path | None,
        job_type: str,
        current_message: str,
    ) -> str:
        lines = [
            "# Current Task",
            "",
            f"- Last active agent: `{agent_name}`",
            f"- Session state: `{state_label}`",
            f"- Job type: `{job_type}`",
            f"- Updated at: `{utcnow().isoformat()}`",
        ]
        if task_id:
            lines.append(f"- Task ID: `{task_id}`")
        if task_path is not None:
            lines.append(f"- Task file: `{self._display_path(task_path)}`")
        if latest_brief_name:
            lines.append(f"- Latest brief: `{latest_brief_name}`")
        lines.extend(["", "## Current message", "", current_message.strip() or "(empty message)"])
        lines.extend(["", "## Next action", "", next_action])
        return "\n".join(lines) + "\n"

    def _extract_handoff_lines(self, handoffs: list[HandoffSpec]) -> list[str]:
        return [
            f"{handoff.task_id or handoff.target_agent}: {handoff.target_agent} -> {trim_text(handoff.title or handoff.body, 220)}"
            for handoff in handoffs
        ]

    def _compact_handoff_blocks(self, handoffs: list[HandoffSpec]) -> list[str]:
        blocks: list[str] = []
        for handoff in handoffs:
            task_path = self._display_path(self.task_file(handoff.task_id)) if handoff.task_id else "TASKS/"
            task_line = f"Task {handoff.task_id}. " if handoff.task_id else ""
            compact_body = (
                f"{task_line}Read `TASK_BOARD.md`, `{task_path}`, `CURRENT_STATE.md`, and the relevant `AGENTS/*.md` notes.\n"
                f"Target summary: {trim_text(handoff.body, 420)}"
            )
            blocks.append(
                f"[[handoff agent=\"{handoff.target_agent}\"]]\n"
                f"{compact_body}\n"
                "[[/handoff]]"
            )
        return blocks

    def _materialize_handoffs(
        self,
        *,
        source_agent: str,
        handoffs: list[HandoffSpec],
        parent_task_id: str | None,
        source_log_name: str,
    ) -> tuple[list[HandoffSpec], list[Path]]:
        index = self._load_task_index()
        updated_files: list[Path] = []
        materialized: list[HandoffSpec] = []
        for handoff in handoffs:
            task_id = handoff.task_id or self._allocate_task_id(index)
            card = index.get(task_id) or TaskCard(
                task_id=task_id,
                title=handoff.title or self._derive_task_title(handoff.body),
                owner=handoff.target_agent,
                status="ready",
                source_agent=source_agent,
                depends_on=parent_task_id,
                created_at=utcnow().isoformat(),
                updated_at=utcnow().isoformat(),
                latest_brief_name=None,
                source_log_name=source_log_name,
                goal=handoff.body,
                definition_of_done=(
                    "Complete the focused next action, update CURRENT_STATE.md / STATUS.md, and leave the work "
                    "in a reviewable state."
                ),
                notes=f"Auto-generated from `{source_agent}` handoff.",
            )
            card.owner = handoff.target_agent
            card.status = "ready"
            card.source_agent = source_agent
            card.depends_on = card.depends_on or parent_task_id
            card.updated_at = utcnow().isoformat()
            card.source_log_name = source_log_name
            card.goal = handoff.body
            card.title = handoff.title or card.title or self._derive_task_title(handoff.body)
            card.notes = f"Latest handoff from `{source_agent}`.\n\n{handoff.body}"
            index[task_id] = card
            self._write_task_card(card)
            updated_files.append(self.task_file(task_id))
            materialized.append(HandoffSpec(handoff.target_agent, handoff.body, task_id, card.title))
        if handoffs:
            self._save_task_index(index)
        updated_files.append(self._refresh_task_board(index))
        return materialized, updated_files

    def _claim_task_for_job(self, *, task_id: str, agent_name: str, user_text: str, brief_name: str) -> Path:
        index = self._load_task_index()
        card = index.get(task_id) or TaskCard(
            task_id=task_id,
            title=self._derive_task_title(user_text),
            owner=agent_name,
            status="in_progress",
            source_agent="operator",
            depends_on=None,
            created_at=utcnow().isoformat(),
            updated_at=utcnow().isoformat(),
            latest_brief_name=brief_name,
            source_log_name=None,
            goal=user_text.strip() or "Continue the referenced task.",
            definition_of_done="Complete the requested work and update the local session artifacts.",
            notes="Task card was claimed from an incoming job.",
        )
        card.owner = agent_name
        card.status = "in_progress"
        card.updated_at = utcnow().isoformat()
        card.latest_brief_name = brief_name
        index[task_id] = card
        self._save_task_index(index)
        self._write_task_card(card)
        self._refresh_task_board(index)
        return self.task_file(task_id)

    def _finalize_current_task(
        self,
        *,
        task_id: str | None,
        agent_name: str,
        report_text: str,
        question_blocks: list[str],
        handoffs: list[HandoffSpec],
        raw_log_name: str,
    ) -> list[Path]:
        index = self._load_task_index()
        if not task_id or task_id not in index:
            return [self._refresh_task_board(index)]
        card = index[task_id]
        card.owner = agent_name
        card.updated_at = utcnow().isoformat()
        card.source_log_name = raw_log_name
        if question_blocks:
            card.status = "blocked_on_operator"
            card.notes = question_blocks[0]
        elif handoffs:
            card.status = "handoff_queued"
            card.notes = ", ".join(
                f"{handoff.task_id or 'task-pending'}->{handoff.target_agent}"
                for handoff in handoffs
            )
        else:
            card.status = "done"
            card.notes = report_text or "Marked done by runtime."
        index[task_id] = card
        self._save_task_index(index)
        self._write_task_card(card)
        return [self.task_file(task_id), self._refresh_task_board(index)]

    def _mark_task_failed(self, *, task_id: str | None, agent_name: str, summary: str, raw_log_name: str) -> list[Path]:
        index = self._load_task_index()
        if not task_id:
            return [self._refresh_task_board(index)]
        card = index.get(task_id) or TaskCard(
            task_id=task_id,
            title=self._derive_task_title(summary),
            owner=agent_name,
            status="failed",
            source_agent=agent_name,
            depends_on=None,
            created_at=utcnow().isoformat(),
            updated_at=utcnow().isoformat(),
            latest_brief_name=None,
            source_log_name=raw_log_name,
            goal="Investigate the failed runtime step.",
            definition_of_done="Stabilize the task and update the task board.",
            notes=summary,
        )
        card.owner = agent_name
        card.status = "failed"
        card.updated_at = utcnow().isoformat()
        card.source_log_name = raw_log_name
        card.notes = summary
        index[task_id] = card
        self._save_task_index(index)
        self._write_task_card(card)
        return [self.task_file(task_id), self._refresh_task_board(index)]

    def _reconcile_task_index(self, index: dict[str, TaskCard]) -> set[str]:
        children_by_parent: dict[str, list[str]] = {}
        for card in index.values():
            if card.depends_on and card.depends_on in index:
                children_by_parent.setdefault(card.depends_on, []).append(card.task_id)

        changed_task_ids: set[str] = set()
        while True:
            loop_changed = False
            for parent_id, child_ids in sorted(children_by_parent.items()):
                parent = index.get(parent_id)
                if parent is None:
                    continue
                children = [index[child_id] for child_id in child_ids if child_id in index]
                if not children:
                    continue

                next_status, next_notes = self._roll_up_parent_status(parent=parent, children=children)
                if next_status == parent.status and next_notes == parent.notes:
                    continue

                parent.status = next_status
                parent.notes = next_notes
                parent.updated_at = utcnow().isoformat()
                changed_task_ids.add(parent_id)
                loop_changed = True

            if not loop_changed:
                break

        return changed_task_ids

    def _roll_up_parent_status(self, *, parent: TaskCard, children: list[TaskCard]) -> tuple[str, str]:
        blocked_children = [child.task_id for child in children if child.status == "blocked_on_operator"]
        failed_children = [child.task_id for child in children if child.status == "failed"]
        active_children = [child.task_id for child in children if child.status in ACTIVE_CHILD_TASK_STATUSES]

        if blocked_children:
            return (
                "blocked_on_operator",
                f"Waiting on operator response for child task(s): {', '.join(blocked_children)}",
            )
        if active_children:
            return (
                "handoff_queued",
                f"Waiting on child task(s): {', '.join(active_children)}",
            )
        if failed_children:
            return (
                "failed",
                f"Child task failed: {', '.join(failed_children)}",
            )
        if children and all(child.status == "done" for child in children):
            return (
                "done",
                f"Completed via child task(s): {', '.join(child.task_id for child in children)}",
            )
        return parent.status, parent.notes

    def _load_task_index(self) -> dict[str, TaskCard]:
        self.ensure_structure()
        raw = self.task_index_file.read_text(encoding="utf-8").strip() or "{}"
        data = json.loads(raw)
        allowed_fields = set(TaskCard.__dataclass_fields__)
        index: dict[str, TaskCard] = {}
        changed = False
        for task_id, payload in data.items():
            if not isinstance(payload, dict):
                changed = True
                continue
            sanitized = {key: value for key, value in payload.items() if key in allowed_fields}
            if sanitized.keys() != payload.keys():
                changed = True
            if "task_id" not in sanitized:
                sanitized["task_id"] = str(task_id).strip().upper()
                changed = True
            index[task_id] = TaskCard(**sanitized)
        if changed:
            self._save_task_index(index)
        return index

    def _save_task_index(self, index: dict[str, TaskCard]) -> None:
        payload = {task_id: asdict(card) for task_id, card in index.items()}
        self.task_index_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _refresh_task_board(self, index: dict[str, TaskCard] | None = None, *, reconcile: bool = True) -> Path:
        task_index = index or self._load_task_index()
        if reconcile:
            changed_task_ids = self._reconcile_task_index(task_index)
            if changed_task_ids:
                self._save_task_index(task_index)
                for task_id in sorted(changed_task_ids):
                    self._write_task_card(task_index[task_id])
        if not task_index:
            self.task_board_file.write_text(self._build_task_board_template(), encoding="utf-8")
            return self.task_board_file
        ordered = sorted(
            task_index.values(),
            key=lambda card: (
                TASK_STATUS_ORDER.index(card.status) if card.status in TASK_STATUS_ORDER else len(TASK_STATUS_ORDER),
                card.task_id,
            ),
        )
        lines = [
            "# Task Board",
            "",
            f"- Session: `{self.session_name}`",
            f"- Session ID: `{self.session_id}`",
            f"- Workspace root: `{self.relative_root.as_posix()}`",
            f"- Updated at: `{utcnow().isoformat()}`",
            "",
        ]
        for status in TASK_STATUS_ORDER:
            cards = [card for card in ordered if card.status == status]
            if not cards:
                continue
            lines.extend([f"## {status}", ""])
            for card in cards:
                depends = card.depends_on or "none"
                lines.append(
                    f"- `{card.task_id}` | owner=`{card.owner}` | depends_on=`{depends}` | "
                    f"[{self._display_path(self.task_file(card.task_id))}] {trim_text(card.title, 90)}"
                )
            lines.append("")
        self.task_board_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return self.task_board_file

    def _write_task_card(self, card: TaskCard) -> None:
        self.task_file(card.task_id).write_text(
            (
                f"# {card.task_id} {card.title}\n\n"
                f"- Status: `{card.status}`\n"
                f"- Owner: `{card.owner}`\n"
                f"- Source agent: `{card.source_agent}`\n"
                f"- Depends on: `{card.depends_on or 'none'}`\n"
                f"- Created at: `{card.created_at}`\n"
                f"- Updated at: `{card.updated_at}`\n"
                f"- Latest brief: `{card.latest_brief_name or 'none'}`\n"
                f"- Source log: `{card.source_log_name or 'none'}`\n\n"
                "## Goal\n\n"
                f"{card.goal.strip() or '(none)'}\n\n"
                "## Definition Of Done\n\n"
                f"{card.definition_of_done.strip() or '(none)'}\n\n"
                "## Notes\n\n"
                f"{card.notes.strip() or '(none)'}\n"
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _allocate_task_id(index: dict[str, TaskCard]) -> str:
        values = [int(task_id.split("-")[1]) for task_id in index]
        next_index = (max(values) + 1) if values else 1
        return f"T-{next_index:03d}"

    @staticmethod
    def _derive_task_title(text: str) -> str:
        stripped = " ".join(text.strip().split())
        if not stripped:
            return "Focused follow-up task"
        stripped = re.sub(r"^Task\s+T-\d{3}\.?\s*", "", stripped, flags=re.IGNORECASE)
        return trim_text(stripped, 72)

    @staticmethod
    def _extract_task_id(text: str) -> str | None:
        match = TASK_ID_RE.search(text or "")
        return match.group(1).upper() if match else None

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def _bridge_summary_text(
        *,
        session_status: str,
        pending_jobs: int,
        active_jobs: int,
        active_agent_name: str | None = None,
        active_task_id: str | None = None,
    ) -> str:
        if active_jobs > 0:
            active_agent = active_agent_name or "active worker"
            if active_task_id:
                return (
                    f"Bridge session summary says `{active_agent}` is working on `{active_task_id}` "
                    f"with {active_jobs} active job(s) and {pending_jobs} pending job(s)."
                )
            return (
                f"Bridge session summary says `{active_agent}` is processing active work "
                f"with {active_jobs} active job(s) and {pending_jobs} pending job(s)."
            )
        if session_status == "ready":
            return "Bridge session summary says the session is ready and no jobs are active."
        if session_status == "paused":
            return "Bridge session summary says the session is paused and waiting for an explicit resume."
        if session_status == "closed":
            return "Bridge session summary says the session is closed."
        if session_status == "failed_start":
            return "Bridge session summary says startup failed before workers attached."
        return (
            f"Bridge session summary says status={session_status}, pending_jobs={pending_jobs}, "
            f"active_jobs={active_jobs}."
        )

    @staticmethod
    def _bridge_next_action(
        *,
        session_status: str,
        desired_status: str,
        attached_workers: int,
        total_agents: int,
        active_agent_name: str | None = None,
    ) -> str:
        if active_agent_name:
            return f"Wait for {active_agent_name} to finish the active task."
        if session_status == "waiting_for_workers":
            return f"Wait for workers to attach ({attached_workers}/{total_agents})."
        if session_status == "awaiting_launcher":
            return "Wait for the launcher to reconnect."
        if session_status == "failed_start":
            return "Start a fresh session after checking the execution plane."
        if session_status == "closed":
            return "No further action. The session is closed."
        if session_status == "paused" or desired_status == "paused":
            return "Wait for an explicit resume request."
        return "Waiting for the next user message or job."

    @staticmethod
    def _extract_backticked_field(text: str, label: str) -> str | None:
        match = re.search(
            rf"^- {re.escape(label)}: `(?P<value>.+?)`$",
            text,
            re.MULTILINE,
        )
        if match is None:
            return None
        return match.group("value").strip() or None

    @staticmethod
    def _write_if_missing(path: Path, content: str) -> None:
        if path.exists():
            return
        path.write_text(content, encoding="utf-8")
