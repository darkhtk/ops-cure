"""Systematic protocol validation: 10 dialogue scenarios.

Each scenario exercises a distinct protocol facet:

  01. inquiry: simple question -> answer -> close(answered)
  02. inquiry: auto-abandoned at tier-3 (24h, system close)
  03. proposal: accepted with multiple agreements
  04. proposal: rejected after explicit objection
  05. proposal: superseded with parent_conversation_id link
  06. task: happy path with full evidence trail
  07. task: failure auto-closes the conversation
  08. task: lease expiration lets a different actor take over
  09. proposal: handoff chain alice -> bob -> carol -> close
  10. multi-conversation: 3 conversations in 1 room track independently

Run:  python scripts/protocol_scenarios.py

Each scenario prints its transcript, runs assertions at the end, and
records a pass/fail in the final summary. A non-zero exit code means
at least one scenario regressed -- run as part of a release gate.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
NAS_BRIDGE_ROOT = REPO_ROOT / "nas_bridge"
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))


_TMP_DIR = tempfile.mkdtemp(prefix="opscure_scenarios_")
os.environ["BRIDGE_SHARED_AUTH_TOKEN"] = "demo-token"
os.environ["BRIDGE_DISABLE_DISCORD"] = "true"
os.environ["BRIDGE_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR, 'scenarios.db').as_posix()}"

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
    ChatTaskFailRequest,
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
from app.models import RemoteTaskAssignmentModel, ResourceLeaseModel  # noqa: E402
from app.services.remote_task_service import RemoteTaskService  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


# ---------------------------------------------------------------------------
# Harness


class StubThreadManager:
    def __init__(self) -> None:
        self.created_threads: list[str] = []

    async def create_thread(
        self, *, guild_id, parent_channel_id, title, starter_text, auto_archive_duration,
    ) -> str:
        del guild_id, parent_channel_id, starter_text, auto_archive_duration
        thread_id = f"discord-{title.replace(' ', '-')}-{len(self.created_threads) + 1}"
        self.created_threads.append(thread_id)
        return thread_id

    async def post_message(self, thread_id, content):
        del thread_id, content
        return [("msg-stub", "")]


class Transcript:
    def __init__(self) -> None:
        self.indent = "    "

    def write(self, actor: str, kind: str, detail: str, conversation: str = "") -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        prefix = f"[{conversation}] " if conversation else ""
        print(f"{self.indent}{ts}  {actor:<12}  {kind:<28}  {prefix}{detail}")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def short(value: str | None) -> str:
    if not value:
        return ""
    return value[:8]


def backdate_conversation(conversation_id: str, *, minutes: int) -> None:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with db_module.session_scope() as session:
        row = session.get(ChatConversationModel, conversation_id)
        if row is not None:
            row.created_at = moment
            row.last_speech_at = moment


def expire_lease(task_id: str) -> None:
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    with db_module.session_scope() as session:
        assignment = session.scalar(
            select(RemoteTaskAssignmentModel)
            .where(RemoteTaskAssignmentModel.task_id == task_id)
            .where(RemoteTaskAssignmentModel.released_at.is_(None))
        )
        if assignment is not None:
            assignment.lease_expires_at = expired
            assignment.status = "released"
            assignment.released_at = expired
        lease = session.scalar(
            select(ResourceLeaseModel)
            .where(ResourceLeaseModel.resource_kind == "remote_task")
            .where(ResourceLeaseModel.resource_id == task_id)
        )
        if lease is not None:
            lease.expires_at = expired
            lease.released_at = expired
            lease.status = "released"


class Actor:
    def __init__(self, name, *, conv, coord, transcript):
        self.name = name
        self.conv = conv
        self.coord = coord
        self.transcript = transcript

    def open_inquiry(self, thread, title, *, intent=None, addressed_to=None):
        result = self.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="inquiry", title=title, opener_actor=self.name,
                intent=intent, addressed_to=addressed_to,
            ),
        )
        addr = f"  -> @{addressed_to}" if addressed_to else ""
        self.transcript.write(self.name, "conversation.opened", f'inquiry "{title}"{addr}', conversation=short(result.id))
        return result

    def open_proposal(self, thread, title, *, intent=None, owner=None, parent_id=None):
        result = self.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="proposal", title=title, opener_actor=self.name,
                intent=intent, owner_actor=owner,
                parent_conversation_id=parent_id,
            ),
        )
        owner_str = f"  owner=@{owner}" if owner else ""
        parent_str = f"  parent={short(parent_id)}" if parent_id else ""
        self.transcript.write(
            self.name, "conversation.opened",
            f'proposal "{title}"{owner_str}{parent_str}', conversation=short(result.id),
        )
        return result

    def open_task(self, thread, title, *, objective, success_criteria=None):
        result = self.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="task", title=title, opener_actor=self.name,
                objective=objective, success_criteria=success_criteria or {},
            ),
        )
        self.transcript.write(
            self.name, "conversation.opened",
            f'task "{title}"  bound={short(result.bound_task_id)}',
            conversation=short(result.id),
        )
        return result

    def speak(self, conv_id, kind, content, *, addressed_to=None):
        self.conv.submit_speech(
            conversation_id=conv_id,
            request=SpeechActSubmitRequest(
                actor_name=self.name, kind=kind, content=content,
                addressed_to=addressed_to,
            ),
        )
        addr = f"  @{addressed_to}" if addressed_to else ""
        self.transcript.write(self.name, f"speech.{kind}", f'"{content}"{addr}', conversation=short(conv_id))

    def close(self, conv_id, resolution, summary=None):
        self.conv.close_conversation(
            conversation_id=conv_id, closed_by=self.name,
            resolution=resolution, summary=summary,
        )
        msg = f"resolution={resolution}"
        if summary:
            msg += f'  "{summary}"'
        self.transcript.write(self.name, "conversation.closed", msg, conversation=short(conv_id))

    def handoff(self, conv_id, *, new_owner, reason=None):
        self.conv.transfer_owner(
            conversation_id=conv_id, by_actor=self.name,
            new_owner=new_owner, reason=reason,
        )
        msg = f"new_owner=@{new_owner}"
        if reason:
            msg += f"  ({reason})"
        self.transcript.write(self.name, "conversation.handoff", msg, conversation=short(conv_id))

    def claim(self, conv_id, *, lease_seconds=120):
        response = self.coord.claim(
            conversation_id=conv_id,
            request=ChatTaskClaimRequest(actor_name=self.name, lease_seconds=lease_seconds),
        )
        token = response.task["current_assignment"]["lease_token"]
        self.transcript.write(
            self.name, "task.claimed",
            f"lease={lease_seconds}s  status={response.task['status']}",
            conversation=short(conv_id),
        )
        return token

    def heartbeat(self, conv_id, *, lease_token, phase, summary=None, **metrics):
        self.coord.heartbeat(
            conversation_id=conv_id,
            request=ChatTaskHeartbeatRequest(
                actor_name=self.name, lease_token=lease_token,
                phase=phase, summary=summary, **metrics,
            ),
        )
        bits = []
        for k, v in metrics.items():
            if v:
                bits.append(f"{k}={v}")
        bit_str = ("  " + " ".join(bits)) if bits else ""
        sum_str = f'  "{summary}"' if summary else ""
        self.transcript.write(
            self.name, "task.heartbeat", f"phase={phase}{bit_str}{sum_str}",
            conversation=short(conv_id),
        )

    def evidence(self, conv_id, *, lease_token, kind, summary, payload=None):
        self.coord.add_evidence(
            conversation_id=conv_id,
            request=ChatTaskEvidenceRequest(
                actor_name=self.name, lease_token=lease_token,
                kind=kind, summary=summary, payload=payload or {},
            ),
        )
        self.transcript.write(self.name, "task.evidence", f'{kind}: "{summary}"', conversation=short(conv_id))

    def complete(self, conv_id, *, lease_token, summary=None):
        self.coord.complete(
            conversation_id=conv_id,
            request=ChatTaskCompleteRequest(
                actor_name=self.name, lease_token=lease_token, summary=summary,
            ),
        )
        self.transcript.write(self.name, "task.completed", f'"{summary or ""}"', conversation=short(conv_id))
        self.transcript.write(
            "system", "conversation.closed",
            "resolution=completed (auto, task complete)", conversation=short(conv_id),
        )

    def fail(self, conv_id, *, lease_token, error_text):
        self.coord.fail(
            conversation_id=conv_id,
            request=ChatTaskFailRequest(
                actor_name=self.name, lease_token=lease_token, error_text=error_text,
            ),
        )
        self.transcript.write(self.name, "task.failed", f'"{error_text}"', conversation=short(conv_id))
        self.transcript.write(
            "system", "conversation.closed",
            "resolution=failed (auto, task fail)", conversation=short(conv_id),
        )


class Env:
    def __init__(self):
        self.thread_manager = StubThreadManager()
        self.chat = ChatBehaviorService(thread_manager=self.thread_manager)
        self.presence = PresenceService()
        self.approvals = KernelApprovalService()
        self.remote_task = RemoteTaskService(
            presence_service=self.presence,
            kernel_approval_service=self.approvals,
        )
        self.conv = ChatConversationService(remote_task_service=self.remote_task)
        self.coord = ChatTaskCoordinator(
            conversation_service=self.conv,
            remote_task_service=self.remote_task,
        )

    def open_thread(self, title="protocol-scenarios"):
        async def go():
            return await self.chat.create_chat_thread(
                guild_id="guild-test",
                parent_channel_id="parent-test",
                title=title,
                topic=None,
                created_by="system",
            )
        return asyncio.run(go())

    def actor(self, name, transcript):
        return Actor(name, conv=self.conv, coord=self.coord, transcript=transcript)


def get_conversation_state(conversation_id: str) -> dict[str, Any]:
    with db_module.session_scope() as session:
        row = session.get(ChatConversationModel, conversation_id)
        if row is None:
            return {}
        return {
            "kind": row.kind,
            "state": row.state,
            "resolution": row.resolution,
            "owner_actor": row.owner_actor,
            "expected_speaker": row.expected_speaker,
            "is_general": bool(row.is_general),
            "idle_warning_count": row.idle_warning_count or 0,
            "speech_count": row.speech_count or 0,
            "unaddressed_speech_count": row.unaddressed_speech_count or 0,
            "bound_task_id": row.bound_task_id,
            "closed_by": row.closed_by,
            "parent_conversation_id": row.parent_conversation_id,
        }


def count_events(conversation_id: str, event_kind: str) -> int:
    with db_module.session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == conversation_id)
            .where(ChatMessageModel.event_kind == event_kind)
        ) or 0


# ---------------------------------------------------------------------------
# Scenarios

def scenario_01_inquiry_simple(env, transcript):
    """alice asks bob a question, bob answers, alice closes as answered."""
    section("Scenario 01 — inquiry: simple Q&A")
    thread = env.open_thread("s01")
    alice = env.actor("alice", transcript)
    bob = env.actor("bob", transcript)

    inquiry = alice.open_inquiry(
        thread, "What's the rotation policy for auth tokens?",
        intent="Need answer for runbook", addressed_to="bob",
    )
    # bob (the expected_speaker) answers without explicitly addressing
    # back; the protocol clears expected_speaker because the round
    # has resolved.
    bob.speak(inquiry.id, "answer", "Rotates every 90 days; last on 2026-04-15")
    alice.close(inquiry.id, "answered", "Logged in runbook v2")

    state = get_conversation_state(inquiry.id)
    assert state["kind"] == "inquiry"
    assert state["state"] == "closed"
    assert state["resolution"] == "answered"
    assert state["closed_by"] == "alice"
    # expected_speaker was bob initially; cleared when bob (expected)
    # spoke unaddressed.
    assert state["expected_speaker"] is None


def scenario_02_inquiry_auto_abandoned(env, transcript):
    """nobody answers in 25h; sweep_idle fires tier-1, tier-2, then auto-abandons."""
    section("Scenario 02 — inquiry: auto-abandoned at tier-3")
    thread = env.open_thread("s02")
    alice = env.actor("alice", transcript)

    inquiry = alice.open_inquiry(
        thread, "Anyone seen the migration script?",
        addressed_to="bob",
    )
    backdate_conversation(inquiry.id, minutes=25 * 60)
    transcript.write("system", "(simulated)", "backdated 25h to trigger tier-3", conversation=short(inquiry.id))

    flagged = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    for f in flagged:
        transcript.write(
            "system", "idle_warning -> abandoned",
            f"tier_count={f.idle_warning_count} resolution={f.resolution}",
            conversation=short(f.id),
        )

    state = get_conversation_state(inquiry.id)
    assert state["state"] == "closed"
    assert state["resolution"] == "abandoned"
    assert state["closed_by"] == "system"
    assert state["idle_warning_count"] == 3
    # tier-1 + tier-2 warning rows must exist (tier-3 closes via close event)
    assert count_events(inquiry.id, "chat.conversation.idle_warning") == 2


def scenario_03_proposal_accepted_multi_agreement(env, transcript):
    """alice proposes, bob agrees, carol agrees, alice closes accepted."""
    section("Scenario 03 — proposal: accepted with multiple agreements")
    thread = env.open_thread("s03")
    alice = env.actor("alice", transcript)
    bob = env.actor("bob", transcript)
    carol = env.actor("carol", transcript)

    proposal = alice.open_proposal(
        thread, "Adopt evidence-required heartbeats org-wide",
        intent="Stop fake progress claims", owner="alice",
    )
    bob.speak(proposal.id, "agree", "+1 -- AI ops handbook needs this")
    carol.speak(proposal.id, "agree", "+1 -- I'll draft the policy doc")
    alice.close(proposal.id, "accepted", "Adopted; carol drafts policy")

    state = get_conversation_state(proposal.id)
    assert state["resolution"] == "accepted"
    assert state["state"] == "closed"
    # 2 agree speech rows
    assert count_events(proposal.id, "chat.speech.agree") == 2


def scenario_04_proposal_rejected_with_objection(env, transcript):
    """alice proposes, bob objects, alice closes rejected."""
    section("Scenario 04 — proposal: rejected after objection")
    thread = env.open_thread("s04")
    alice = env.actor("alice", transcript)
    bob = env.actor("bob", transcript)

    proposal = alice.open_proposal(
        thread, "Switch all repos to single-branch trunk",
        owner="alice",
    )
    bob.speak(proposal.id, "object", "blocks our staged-rollout flow on growth-platform")
    alice.speak(proposal.id, "agree", "fair point; will revisit per-repo")
    alice.close(proposal.id, "rejected", "rolled back due to staged-rollout dependency")

    state = get_conversation_state(proposal.id)
    assert state["resolution"] == "rejected"
    assert count_events(proposal.id, "chat.speech.object") == 1


def scenario_05_proposal_superseded_with_parent_link(env, transcript):
    """alice proposes v1, bob proposes v2 with parent_conversation_id link.
    alice closes v1 as superseded; v2 stays open."""
    section("Scenario 05 — proposal: v1 superseded by v2 (parent_conversation_id)")
    thread = env.open_thread("s05")
    alice = env.actor("alice", transcript)
    bob = env.actor("bob", transcript)

    v1 = alice.open_proposal(thread, "Heartbeat every 30s", owner="alice")
    bob.speak(v1.id, "object", "30s is too chatty under WAN; proposing alternative")
    v2 = bob.open_proposal(
        thread, "Heartbeat every 60s + on phase change",
        owner="bob", parent_id=v1.id,
    )
    alice.close(v1.id, "superseded", f"replaced by {short(v2.id)}")

    state_v1 = get_conversation_state(v1.id)
    state_v2 = get_conversation_state(v2.id)
    assert state_v1["resolution"] == "superseded"
    assert state_v2["state"] == "open"
    assert state_v2["parent_conversation_id"] == v1.id


def scenario_06_task_happy_path(env, transcript):
    """alice opens task, codex-pca claims, heartbeats, evidences (file+test),
    completes with auto-close."""
    section("Scenario 06 — task: happy path with full evidence trail")
    thread = env.open_thread("s06")
    alice = env.actor("alice", transcript)
    pca = env.actor("codex-pca", transcript)

    task = alice.open_task(
        thread, "Refactor auth middleware",
        objective="Replace legacy session token storage; keep public API stable",
        success_criteria={"required": ["all tests pass", "no API breakage"]},
    )
    lease = pca.claim(task.id, lease_seconds=180)
    pca.heartbeat(task.id, lease_token=lease, phase="executing",
                  summary="reading current code", files_read_count=4)
    pca.evidence(task.id, lease_token=lease, kind="file_write",
                 summary="patched nas_bridge/app/auth/middleware.py",
                 payload={"files": ["nas_bridge/app/auth/middleware.py"]})
    pca.heartbeat(task.id, lease_token=lease, phase="executing",
                  summary="running tests", tests_run_count=12)
    pca.evidence(task.id, lease_token=lease, kind="test_result",
                 summary="pytest -- 12 passed",
                 payload={"passed": 12, "failed": 0})
    pca.complete(task.id, lease_token=lease,
                 summary="all 12 auth tests pass; new token store wired in")

    state = get_conversation_state(task.id)
    assert state["resolution"] == "completed"
    assert state["state"] == "closed"
    assert state["bound_task_id"] is not None
    assert count_events(task.id, "chat.task.heartbeat") == 2
    assert count_events(task.id, "chat.task.evidence") == 2
    assert count_events(task.id, "chat.task.completed") == 1


def scenario_07_task_fail(env, transcript):
    """task fails; conversation auto-closes as failed."""
    section("Scenario 07 — task: failure auto-closes the conversation")
    thread = env.open_thread("s07")
    alice = env.actor("alice", transcript)
    pcb = env.actor("claude-pcb", transcript)

    task = alice.open_task(
        thread, "Run prod migration",
        objective="apply schema migration v42 to prod",
    )
    lease = pcb.claim(task.id, lease_seconds=120)
    pcb.heartbeat(task.id, lease_token=lease, phase="executing",
                  summary="starting migration")
    pcb.evidence(task.id, lease_token=lease, kind="error",
                 summary="ALTER TABLE failed: duplicate column 'auth_token_v2'",
                 payload={"sqlcode": "42701"})
    pcb.fail(task.id, lease_token=lease, error_text="migration aborted: duplicate column")

    state = get_conversation_state(task.id)
    assert state["resolution"] == "failed"
    assert state["state"] == "closed"
    assert count_events(task.id, "chat.task.failed") == 1


def scenario_08_task_lease_takeover(env, transcript):
    """A claims with short lease, walks away, lease expires, B takes over and completes."""
    section("Scenario 08 — task: lease expiration lets a different actor take over")
    thread = env.open_thread("s08")
    alice = env.actor("alice", transcript)
    pca = env.actor("codex-pca", transcript)
    pcb = env.actor("claude-pcb", transcript)

    task = alice.open_task(
        thread, "Sweep stale build artifacts",
        objective="rm -rf old build dirs older than 7d on NAS",
    )
    lease_a = pca.claim(task.id, lease_seconds=30)
    pca.heartbeat(task.id, lease_token=lease_a, phase="executing",
                  summary="enumerating dirs")
    transcript.write("system", "(simulated)", "pca walks away; lease expires", conversation=short(task.id))
    expire_lease(task.bound_task_id)
    lease_b = pcb.claim(task.id, lease_seconds=60)
    pcb.evidence(task.id, lease_token=lease_b, kind="command_execution",
                 summary="rm -rf /volume1/build/old/* (7d+)",
                 payload={"freed_bytes": 1234567890})
    pcb.complete(task.id, lease_token=lease_b, summary="reclaimed ~1.2GB")

    state = get_conversation_state(task.id)
    assert state["resolution"] == "completed"
    assert state["owner_actor"] == "claude-pcb"  # last claimant


def scenario_09_proposal_handoff_chain(env, transcript):
    """alice opens, hands to bob, bob hands to carol, carol closes."""
    section("Scenario 09 — proposal: handoff chain alice -> bob -> carol -> close")
    thread = env.open_thread("s09")
    alice = env.actor("alice", transcript)
    bob = env.actor("bob", transcript)
    carol = env.actor("carol", transcript)

    proposal = alice.open_proposal(
        thread, "Adopt OpenTelemetry for bridge",
        intent="Replace ad-hoc loggers", owner="alice",
    )
    alice.handoff(proposal.id, new_owner="bob", reason="bob owns observability")
    bob.speak(proposal.id, "claim", "drafting OTel config; parking it on me for now")
    bob.handoff(proposal.id, new_owner="carol", reason="carol has the SRE budget call")
    carol.speak(proposal.id, "claim", "approved; will land in next sprint")
    carol.close(proposal.id, "accepted", "OTel landing next sprint")

    state = get_conversation_state(proposal.id)
    assert state["owner_actor"] == "carol"
    assert state["resolution"] == "accepted"
    assert state["closed_by"] == "carol"
    assert count_events(proposal.id, "chat.conversation.handoff") == 2


def scenario_10_multi_conversation_isolation(env, transcript):
    """In one room: 1 task running + 1 inquiry pending + general chat.
    Verify each tracks independently and idle on inquiry doesn't bleed
    into the task."""
    section("Scenario 10 — multi-conversation: isolation in one room")
    thread = env.open_thread("s10")
    alice = env.actor("alice", transcript)
    pca = env.actor("codex-pca", transcript)
    bob = env.actor("bob", transcript)

    # casual general chat
    env.chat.submit_participant_message(
        thread_id=thread.discord_thread_id, actor_name="alice", actor_kind="human",
        content="morning, room",
    )
    transcript.write("alice", "speech.claim", '"morning, room"', conversation="general")

    # an inquiry that will go idle
    inquiry = alice.open_inquiry(thread, "Anyone has the SLO doc?", addressed_to="bob")

    # a task that proceeds normally
    task = alice.open_task(thread, "Bump pydantic to 2.6", objective="upgrade lib + tests")
    lease = pca.claim(task.id, lease_seconds=180)
    pca.evidence(task.id, lease_token=lease, kind="file_write", summary="bumped requirements.txt")
    pca.complete(task.id, lease_token=lease, summary="upgrade clean")

    # idle the inquiry to tier-1 only (don't auto-abandon — verify isolation)
    backdate_conversation(inquiry.id, minutes=35)
    flagged = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id, idle_threshold_seconds=30 * 60,
    )
    for f in flagged:
        transcript.write(
            "system", "idle_warning",
            f"level={f.idle_warning_count} conv={short(f.id)}",
            conversation=short(f.id),
        )

    # bob finally answers, alice closes
    bob.speak(inquiry.id, "answer", "It's at /volume1/docs/slo-2026.md", addressed_to="alice")
    alice.close(inquiry.id, "answered", "found")

    state_task = get_conversation_state(task.id)
    state_inquiry = get_conversation_state(inquiry.id)

    # Task closed completed, untouched by inquiry's idle
    assert state_task["resolution"] == "completed"
    assert state_task["idle_warning_count"] == 0  # task didn't see inquiry's idle
    # Inquiry: tier-1 warning fired but resolved by bob's answer + alice's close
    assert state_inquiry["resolution"] == "answered"
    assert state_inquiry["idle_warning_count"] == 1  # tier-1 fired before answer

    # General conversation still open and got the casual message
    with db_module.session_scope() as session:
        general = session.scalar(
            select(ChatConversationModel)
            .where(ChatConversationModel.thread_id == thread.id)
            .where(ChatConversationModel.is_general.is_(True))
        )
        assert general is not None
        assert general.state == "open"
        assert (general.speech_count or 0) >= 1


SCENARIOS: list[tuple[str, Callable[[Env, Transcript], None]]] = [
    ("01 inquiry simple", scenario_01_inquiry_simple),
    ("02 inquiry auto-abandoned tier-3", scenario_02_inquiry_auto_abandoned),
    ("03 proposal accepted multi-agreement", scenario_03_proposal_accepted_multi_agreement),
    ("04 proposal rejected with objection", scenario_04_proposal_rejected_with_objection),
    ("05 proposal superseded with parent link", scenario_05_proposal_superseded_with_parent_link),
    ("06 task happy path", scenario_06_task_happy_path),
    ("07 task fail", scenario_07_task_fail),
    ("08 task lease takeover", scenario_08_task_lease_takeover),
    ("09 proposal handoff chain", scenario_09_proposal_handoff_chain),
    ("10 multi-conversation isolation", scenario_10_multi_conversation_isolation),
]


def main() -> int:
    db_module.init_db()
    env = Env()
    transcript = Transcript()

    section("Boot")
    print(f"  tmp db = {os.environ['BRIDGE_DATABASE_URL']}")

    results: list[tuple[str, bool, str]] = []
    for name, fn in SCENARIOS:
        try:
            fn(env, transcript)
            results.append((name, True, ""))
        except AssertionError as exc:
            results.append((name, False, f"AssertionError: {exc}"))
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    section("Summary")
    pass_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - pass_count
    for name, ok, msg in results:
        status = "  ok " if ok else "FAIL"
        line = f"  [{status}] {name}"
        if not ok:
            line += f"  -- {msg}"
        print(line)
    print()
    print(f"  total: {pass_count} passed, {fail_count} failed (of {len(results)})")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
