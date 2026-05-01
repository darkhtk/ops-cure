"""Live walkthrough of the AI 협업룸 protocol (PR1+PR2+PR3).

Run:
    python scripts/demo_collab_room.py

Spins up the chat behavior in-process against a tmp sqlite DB and
scripts three actors (one human, two AI agents) through realistic
collaboration scenarios. No bridge HTTP server, no Discord, no real
LLM -- just direct service calls + a transcript printer so you can see
what each protocol primitive looks like in practice.

Scenarios covered end-to-end:

1. Inquiry: alice asks who's free; codex-pca answers; alice closes.
2. Task: alice opens a refactor task; codex-pca claims it, emits a
   heartbeat and a file_write evidence, then completes -- conversation
   auto-closes.
3. Handoff + idle: alice opens a proposal addressed at bob; alice
   transfers ownership to bob; we then backdate the row to simulate
   60min of silence and call sweep_idle to fire the idle warning.

If any protocol guard fires unexpectedly the script raises -- making
this also a smoke test of the PR1-3 surface.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Bootstrap

REPO_ROOT = Path(__file__).resolve().parent.parent
NAS_BRIDGE_ROOT = REPO_ROOT / "nas_bridge"
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))


_TMP_DIR = tempfile.mkdtemp(prefix="opscure_demo_")
os.environ["BRIDGE_SHARED_AUTH_TOKEN"] = "demo-token"
os.environ["BRIDGE_DISABLE_DISCORD"] = "true"
os.environ["BRIDGE_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR, 'demo.db').as_posix()}"


for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]


import app.config as _config  # noqa: E402

_config.get_settings.cache_clear()

import app.db as db_module  # noqa: E402
from app.behaviors.chat.conversation_schemas import (  # noqa: E402
    ChatTaskClaimRequest,
    ChatTaskCompleteRequest,
    ChatTaskEvidenceRequest,
    ChatTaskHeartbeatRequest,
    ConversationOpenRequest,
    SpeechActSubmitRequest,
)
from app.behaviors.chat.conversation_service import ChatConversationService  # noqa: E402
from app.behaviors.chat.models import (  # noqa: E402
    ChatConversationModel,
    ChatMessageModel,
)
from app.behaviors.chat.service import ChatBehaviorService  # noqa: E402
from app.behaviors.chat.task_coordinator import ChatTaskCoordinator  # noqa: E402
from app.kernel.approvals import KernelApprovalService  # noqa: E402
from app.kernel.presence import PresenceService  # noqa: E402
from app.services.remote_task_service import RemoteTaskService  # noqa: E402
from sqlalchemy import select  # noqa: E402


# ---------------------------------------------------------------------------
# Stub thread manager -- satisfies the small surface ChatBehaviorService uses


class StubThreadManager:
    """Minimum surface to let ChatBehaviorService boot without Discord."""

    def __init__(self) -> None:
        self.created_threads: list[str] = []

    async def create_thread(
        self,
        *,
        guild_id: str,
        parent_channel_id: str,
        title: str,
        starter_text: str,
        auto_archive_duration: int,
    ) -> str:
        del guild_id, parent_channel_id, starter_text, auto_archive_duration
        thread_id = f"discord-{title.replace(' ', '-')}-{len(self.created_threads) + 1}"
        self.created_threads.append(thread_id)
        return thread_id

    async def post_message(self, thread_id: str, content: str):
        del thread_id, content
        return [("msg-stub", "")]


# ---------------------------------------------------------------------------
# Transcript printer


class Transcript:
    """Append-only printer that prints both as-it-happens AND retains
    rows so we can dump a clean replay at the end."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def write(self, actor: str, kind: str, detail: str, conversation: str = "") -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        row = (ts, actor, kind, detail)
        self.rows.append(row)
        prefix = f"[{conversation}] " if conversation else ""
        print(f"  {ts}  {actor:<12}  {kind:<28}  {prefix}{detail}")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Actor -- small wrapper around services that logs every call


class Actor:
    def __init__(
        self,
        name: str,
        *,
        conversations: ChatConversationService,
        coordinator: ChatTaskCoordinator,
        transcript: Transcript,
    ) -> None:
        self.name = name
        self.conversations = conversations
        self.coordinator = coordinator
        self.transcript = transcript

    # -- conversation lifecycle ---------------------------------------------

    def open_inquiry(
        self,
        discord_thread_id: str,
        title: str,
        *,
        intent: str | None = None,
        addressed_to: str | None = None,
    ):
        result = self.conversations.open_conversation(
            discord_thread_id=discord_thread_id,
            request=ConversationOpenRequest(
                kind="inquiry",
                title=title,
                opener_actor=self.name,
                intent=intent,
                addressed_to=addressed_to,
            ),
        )
        addr = f"  -> @{addressed_to}" if addressed_to else ""
        self.transcript.write(self.name, "conversation.opened", f'inquiry "{title}"{addr}', conversation=_short(result.id))
        return result

    def open_proposal(
        self,
        discord_thread_id: str,
        title: str,
        *,
        intent: str | None = None,
        owner_actor: str | None = None,
    ):
        result = self.conversations.open_conversation(
            discord_thread_id=discord_thread_id,
            request=ConversationOpenRequest(
                kind="proposal",
                title=title,
                opener_actor=self.name,
                intent=intent,
                owner_actor=owner_actor,
            ),
        )
        owner = f"  owner=@{owner_actor}" if owner_actor else ""
        self.transcript.write(self.name, "conversation.opened", f'proposal "{title}"{owner}', conversation=_short(result.id))
        return result

    def open_task(
        self,
        discord_thread_id: str,
        title: str,
        *,
        objective: str,
        success_criteria: dict[str, Any] | None = None,
    ):
        result = self.conversations.open_conversation(
            discord_thread_id=discord_thread_id,
            request=ConversationOpenRequest(
                kind="task",
                title=title,
                opener_actor=self.name,
                objective=objective,
                success_criteria=success_criteria or {},
            ),
        )
        self.transcript.write(
            self.name,
            "conversation.opened",
            f'task "{title}"  bound_task={_short(result.bound_task_id or "")}',
            conversation=_short(result.id),
        )
        return result

    def speak(
        self,
        conversation_id: str,
        kind: str,
        content: str,
        *,
        addressed_to: str | None = None,
    ):
        self.conversations.submit_speech(
            conversation_id=conversation_id,
            request=SpeechActSubmitRequest(
                actor_name=self.name,
                kind=kind,
                content=content,
                addressed_to=addressed_to,
            ),
        )
        addr = f"  @{addressed_to}" if addressed_to else ""
        self.transcript.write(self.name, f"speech.{kind}", f'"{content}"{addr}', conversation=_short(conversation_id))

    def close(self, conversation_id: str, resolution: str, summary: str | None = None) -> None:
        self.conversations.close_conversation(
            conversation_id=conversation_id,
            closed_by=self.name,
            resolution=resolution,
            summary=summary,
        )
        msg = f"resolution={resolution}"
        if summary:
            msg += f'  "{summary}"'
        self.transcript.write(self.name, "conversation.closed", msg, conversation=_short(conversation_id))

    def handoff(self, conversation_id: str, *, new_owner: str, reason: str | None = None) -> None:
        self.conversations.transfer_owner(
            conversation_id=conversation_id,
            by_actor=self.name,
            new_owner=new_owner,
            reason=reason,
        )
        msg = f"new_owner=@{new_owner}"
        if reason:
            msg += f'  ({reason})'
        self.transcript.write(self.name, "conversation.handoff", msg, conversation=_short(conversation_id))

    # -- task lifecycle (kind=task only) ------------------------------------

    def claim(self, conversation_id: str, *, lease_seconds: int = 120) -> str:
        response = self.coordinator.claim(
            conversation_id=conversation_id,
            request=ChatTaskClaimRequest(actor_name=self.name, lease_seconds=lease_seconds),
        )
        lease_token = response.task["current_assignment"]["lease_token"]
        self.transcript.write(
            self.name,
            "task.claimed",
            f"lease={lease_seconds}s  status={response.task['status']}",
            conversation=_short(conversation_id),
        )
        return lease_token

    def heartbeat(
        self,
        conversation_id: str,
        *,
        lease_token: str,
        phase: str,
        summary: str | None = None,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
    ) -> None:
        self.coordinator.heartbeat(
            conversation_id=conversation_id,
            request=ChatTaskHeartbeatRequest(
                actor_name=self.name,
                lease_token=lease_token,
                phase=phase,
                summary=summary,
                commands_run_count=commands_run_count,
                files_read_count=files_read_count,
                files_modified_count=files_modified_count,
                tests_run_count=tests_run_count,
            ),
        )
        metrics = []
        if files_read_count:
            metrics.append(f"files_read={files_read_count}")
        if files_modified_count:
            metrics.append(f"files_mod={files_modified_count}")
        if commands_run_count:
            metrics.append(f"cmds={commands_run_count}")
        if tests_run_count:
            metrics.append(f"tests={tests_run_count}")
        metric_str = ("  " + " ".join(metrics)) if metrics else ""
        summary_str = f'  "{summary}"' if summary else ""
        self.transcript.write(
            self.name,
            "task.heartbeat",
            f"phase={phase}{metric_str}{summary_str}",
            conversation=_short(conversation_id),
        )

    def evidence(
        self,
        conversation_id: str,
        *,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.coordinator.add_evidence(
            conversation_id=conversation_id,
            request=ChatTaskEvidenceRequest(
                actor_name=self.name,
                kind=kind,
                summary=summary,
                payload=payload or {},
            ),
        )
        self.transcript.write(
            self.name,
            "task.evidence",
            f'{kind}: "{summary}"',
            conversation=_short(conversation_id),
        )

    def complete(self, conversation_id: str, *, lease_token: str, summary: str | None = None) -> None:
        response = self.coordinator.complete(
            conversation_id=conversation_id,
            request=ChatTaskCompleteRequest(
                actor_name=self.name,
                lease_token=lease_token,
                summary=summary,
            ),
        )
        self.transcript.write(
            self.name,
            "task.completed",
            f'"{summary or ""}"',
            conversation=_short(conversation_id),
        )
        # The coordinator auto-closes the conversation; show that line too.
        self.transcript.write(
            "system",
            "conversation.closed",
            f"resolution={response.conversation.resolution} (auto, task complete)",
            conversation=_short(conversation_id),
        )


# ---------------------------------------------------------------------------
# Helpers


def _short(value: str) -> str:
    return value[:8] if value else ""


def _backdate_conversation(conversation_id: str, minutes: int) -> None:
    backdated = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with db_module.session_scope() as session:
        row = session.get(ChatConversationModel, conversation_id)
        if row is not None:
            row.created_at = backdated
            row.last_speech_at = backdated


def _print_room_summary(thread_uuid: str) -> None:
    print()
    print("Final room state:")
    print(f"  thread = {thread_uuid}")
    with db_module.session_scope() as session:
        rows = list(
            session.scalars(
                select(ChatConversationModel)
                .where(ChatConversationModel.thread_id == thread_uuid)
                .order_by(ChatConversationModel.created_at.asc())
            )
        )
        speech_counts: dict[str, int] = {}
        kind_counts = list(
            session.execute(
                select(ChatMessageModel.event_kind, ChatMessageModel.thread_id)
                .where(ChatMessageModel.thread_id == thread_uuid)
            )
        )
        for event_kind, _ in kind_counts:
            speech_counts[event_kind] = speech_counts.get(event_kind, 0) + 1

        for row in rows:
            badge = "*" if row.is_general else " "
            tail = ""
            if row.state == "closed":
                tail = f"  resolution={row.resolution}"
            elif row.idle_warning_emitted_at is not None:
                tail = "  idle_warning emitted"
            owner = row.owner_actor or "--"
            print(
                f"  {badge} [{row.kind:<8}] {row.state:<6} owner=@{owner:<10} "
                f"speech={row.speech_count:<2}  \"{row.title}\"{tail}"
            )

    print()
    print("Event-kind histogram across the room:")
    for kind, count in sorted(speech_counts.items()):
        print(f"  {count:>3}  {kind}")


# ---------------------------------------------------------------------------
# Main


def main() -> None:
    db_module.init_db()

    thread_manager = StubThreadManager()
    chat_service = ChatBehaviorService(thread_manager=thread_manager)
    presence = PresenceService()
    approvals = KernelApprovalService()
    remote_task = RemoteTaskService(
        presence_service=presence,
        kernel_approval_service=approvals,
    )
    conversation_service = ChatConversationService(remote_task_service=remote_task)
    coordinator = ChatTaskCoordinator(
        conversation_service=conversation_service,
        remote_task_service=remote_task,
    )
    transcript = Transcript()

    section("Boot")
    thread = asyncio.run(
        chat_service.create_chat_thread(
            guild_id="guild-demo",
            parent_channel_id="parent-demo",
            title="opscure-demo",
            topic="ai-collab-room walkthrough",
            created_by="alice",
        ),
    )
    transcript.write("system", "thread.created", f'discord_thread_id={thread.discord_thread_id}')
    transcript.write("system", "conversation.opened", "general (auto)", conversation="general")

    alice = Actor("alice", conversations=conversation_service, coordinator=coordinator, transcript=transcript)
    pca = Actor("codex-pca", conversations=conversation_service, coordinator=coordinator, transcript=transcript)
    bob = Actor("bob", conversations=conversation_service, coordinator=coordinator, transcript=transcript)

    # ---- Scenario 1: inquiry -> answer -> close --------------------------------
    section("Scenario 1: alice asks an inquiry, codex-pca answers")
    inquiry = alice.open_inquiry(
        thread.discord_thread_id,
        "Who has time tonight to refactor the auth middleware?",
        intent="Need someone with ~2h",
        addressed_to="codex-pca",
    )
    pca.speak(
        inquiry.id,
        "answer",
        "I have ~2h free, can take it; need rollback plan first",
        addressed_to="alice",
    )
    alice.speak(
        inquiry.id,
        "claim",
        "Rollback: restore prior middleware.py from main if tests fail",
    )
    alice.close(inquiry.id, "answered", "Owner = codex-pca, rollback plan agreed")

    # ---- Scenario 2: task lifecycle -----------------------------------------
    section("Scenario 2: codex-pca takes a refactor task end-to-end")
    task_conv = alice.open_task(
        thread.discord_thread_id,
        "Refactor auth middleware",
        objective="Replace legacy session token storage; keep public API stable",
        success_criteria={"required": ["all tests pass", "no API breakage"]},
    )
    lease = pca.claim(task_conv.id, lease_seconds=180)
    pca.heartbeat(
        task_conv.id,
        lease_token=lease,
        phase="executing",
        summary="reading current middleware",
        files_read_count=4,
    )
    pca.evidence(
        task_conv.id,
        kind="file_write",
        summary="patched nas_bridge/app/auth/middleware.py to use new token store",
        payload={"files": ["nas_bridge/app/auth/middleware.py"]},
    )
    pca.heartbeat(
        task_conv.id,
        lease_token=lease,
        phase="executing",
        summary="running test suite",
        files_modified_count=1,
        tests_run_count=12,
    )
    pca.evidence(
        task_conv.id,
        kind="test_result",
        summary="pytest tests/test_auth.py -- 12 passed",
        payload={"passed": 12, "failed": 0},
    )
    pca.complete(
        task_conv.id,
        lease_token=lease,
        summary="all 12 auth tests pass; new token store wired in",
    )

    # ---- Scenario 3: handoff + idle warning ---------------------------------
    section("Scenario 3: proposal handoff, then idle warning after 60min silence")
    proposal = alice.open_proposal(
        thread.discord_thread_id,
        "Adopt evidence-required heartbeats org-wide",
        intent="Stop AI-only progress claims",
        owner_actor="alice",
    )
    alice.handoff(proposal.id, new_owner="bob", reason="bob owns the AI ops handbook")
    bob.speak(proposal.id, "claim", "I'll draft the policy update by Friday")
    # Simulate 60min of silence by backdating the row, then sweep.
    _backdate_conversation(proposal.id, minutes=60)
    flagged = conversation_service.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    for item in flagged:
        transcript.write(
            "system",
            "conversation.idle_warning",
            f"silent >=30min (last={item.last_speech_at.isoformat() if item.last_speech_at else '-'})",
            conversation=_short(item.id),
        )
    bob.speak(proposal.id, "answer", "Sorry, draft posted: docs/heartbeats-policy.md")
    bob.close(proposal.id, "accepted", "policy draft merged")

    # ---- Final: room state summary ------------------------------------------
    section("Final room state")
    _print_room_summary(thread.id)


if __name__ == "__main__":
    main()
