"""Ten realistic AI-collaboration scenarios.

Each scenario simulates 2-3 actors (a human + 1-2 AI agents) handling
a real situation that would show up in a dev/ops room: bug triage,
incident response, code review, knowledge transfer, lease recovery
after a worker dies, etc. The transcript reads like an actual room
trace, not a protocol mechanics drill (that's protocol_scenarios.py).

Run:  python scripts/realistic_collab_scenarios.py

The actors:
  alice     -- a human operator / lead
  bob       -- another human (often offline / async)
  claude-pca -- an AI agent on PC-A (deeper code work)
  codex-pcb -- an AI agent on PC-B (ops, build, deploy)

Scenarios:

  01 prod 500 bug -> hotfix (claude triages, codex deploys)
  02 build -> test -> deploy pipeline across two PCs
  03 code review with N+1 objection and follow-up fix
  04 hot incident response (find cause, rollback, post-mortem)
  05 knowledge-sharing inquiry -> docs task
  06 three concurrent tasks in one room running independently
  07 lease takeover when worker dies mid-task
  08 AI defers high-stakes decision to human approval
  09 multi-day async with idle escalation + restart
  10 brainstorm: AIs converge on a consensus design

Each scenario asserts the resulting room state at the end. A
non-zero exit means at least one realistic flow regressed.
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

_TMP_DIR = tempfile.mkdtemp(prefix="opscure_realistic_")
os.environ["BRIDGE_SHARED_AUTH_TOKEN"] = "demo-token"
os.environ["BRIDGE_DISABLE_DISCORD"] = "true"
os.environ["BRIDGE_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR, 'realistic.db').as_posix()}"

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
from app.behaviors.chat.metrics import ChatRoomMetrics  # noqa: E402
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

    async def create_thread(self, *, guild_id, parent_channel_id, title, starter_text, auto_archive_duration) -> str:
        del guild_id, parent_channel_id, starter_text, auto_archive_duration
        thread_id = f"discord-{title.replace(' ', '-')}-{len(self.created_threads) + 1}"
        self.created_threads.append(thread_id)
        return thread_id

    async def post_message(self, thread_id, content):
        del thread_id, content
        return [("msg-stub", "")]


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def short(value: str | None) -> str:
    return (value or "")[:8]


def emit(actor: str, kind: str, detail: str, conversation: str = "") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    prefix = f"[{conversation}] " if conversation else ""
    print(f"    {ts}  {actor:<13}  {kind:<28}  {prefix}{detail}")


def backdate(conversation_id: str, *, minutes: int) -> None:
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
    def __init__(self, name: str, *, conv: ChatConversationService, coord: ChatTaskCoordinator) -> None:
        self.name = name
        self.conv = conv
        self.coord = coord

    def open_inquiry(self, thread, title, *, intent=None, addressed_to=None, parent_id=None):
        result = self.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="inquiry", title=title, opener_actor=self.name,
                intent=intent, addressed_to=addressed_to, parent_conversation_id=parent_id,
            ),
        )
        addr = f"  -> @{addressed_to}" if addressed_to else ""
        parent = f"  parent={short(parent_id)}" if parent_id else ""
        emit(self.name, "conversation.opened", f'inquiry "{title}"{addr}{parent}', short(result.id))
        return result

    def open_proposal(self, thread, title, *, intent=None, owner=None, parent_id=None):
        result = self.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="proposal", title=title, opener_actor=self.name,
                intent=intent, owner_actor=owner, parent_conversation_id=parent_id,
            ),
        )
        owner_str = f"  owner=@{owner}" if owner else ""
        parent_str = f"  parent={short(parent_id)}" if parent_id else ""
        emit(self.name, "conversation.opened", f'proposal "{title}"{owner_str}{parent_str}', short(result.id))
        return result

    def open_task(self, thread, title, *, objective, success_criteria=None, parent_id=None):
        result = self.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="task", title=title, opener_actor=self.name,
                objective=objective, success_criteria=success_criteria or {},
                parent_conversation_id=parent_id,
            ),
        )
        parent_str = f"  parent={short(parent_id)}" if parent_id else ""
        emit(self.name, "conversation.opened",
             f'task "{title}"  bound={short(result.bound_task_id)}{parent_str}',
             short(result.id))
        return result

    def speak(self, conv_id, kind, content, *, addressed_to=None):
        self.conv.submit_speech(
            conversation_id=conv_id,
            request=SpeechActSubmitRequest(
                actor_name=self.name, kind=kind, content=content, addressed_to=addressed_to,
            ),
        )
        addr = f"  @{addressed_to}" if addressed_to else ""
        emit(self.name, f"speech.{kind}", f'"{content}"{addr}', short(conv_id))

    def close(self, conv_id, resolution, summary=None):
        self.conv.close_conversation(
            conversation_id=conv_id, closed_by=self.name, resolution=resolution, summary=summary,
        )
        msg = f"resolution={resolution}"
        if summary:
            msg += f'  "{summary}"'
        emit(self.name, "conversation.closed", msg, short(conv_id))

    def handoff(self, conv_id, *, new_owner, reason=None):
        self.conv.transfer_owner(
            conversation_id=conv_id, by_actor=self.name, new_owner=new_owner, reason=reason,
        )
        msg = f"new_owner=@{new_owner}"
        if reason:
            msg += f"  ({reason})"
        emit(self.name, "conversation.handoff", msg, short(conv_id))

    def claim(self, conv_id, *, lease_seconds=120):
        response = self.coord.claim(
            conversation_id=conv_id,
            request=ChatTaskClaimRequest(actor_name=self.name, lease_seconds=lease_seconds),
        )
        token = response.task["current_assignment"]["lease_token"]
        emit(self.name, "task.claimed", f"lease={lease_seconds}s", short(conv_id))
        return token

    def heartbeat(self, conv_id, *, lease_token, phase, summary=None, **metrics):
        self.coord.heartbeat(
            conversation_id=conv_id,
            request=ChatTaskHeartbeatRequest(
                actor_name=self.name, lease_token=lease_token, phase=phase,
                summary=summary, **metrics,
            ),
        )
        bits = [f"{k}={v}" for k, v in metrics.items() if v]
        bit_str = ("  " + " ".join(bits)) if bits else ""
        sum_str = f'  "{summary}"' if summary else ""
        emit(self.name, "task.heartbeat", f"phase={phase}{bit_str}{sum_str}", short(conv_id))

    def evidence(self, conv_id, *, lease_token, kind, summary, payload=None):
        self.coord.add_evidence(
            conversation_id=conv_id,
            request=ChatTaskEvidenceRequest(
                actor_name=self.name, lease_token=lease_token,
                kind=kind, summary=summary, payload=payload or {},
            ),
        )
        emit(self.name, "task.evidence", f'{kind}: "{summary}"', short(conv_id))

    def complete(self, conv_id, *, lease_token, summary=None):
        self.coord.complete(
            conversation_id=conv_id,
            request=ChatTaskCompleteRequest(
                actor_name=self.name, lease_token=lease_token, summary=summary,
            ),
        )
        emit(self.name, "task.completed", f'"{summary or ""}"', short(conv_id))
        emit("system", "conversation.closed", "resolution=completed (auto)", short(conv_id))

    def fail(self, conv_id, *, lease_token, error_text):
        self.coord.fail(
            conversation_id=conv_id,
            request=ChatTaskFailRequest(
                actor_name=self.name, lease_token=lease_token, error_text=error_text,
            ),
        )
        emit(self.name, "task.failed", f'"{error_text}"', short(conv_id))
        emit("system", "conversation.closed", "resolution=failed (auto)", short(conv_id))


class Env:
    def __init__(self):
        self.thread_manager = StubThreadManager()
        self.chat = ChatBehaviorService(thread_manager=self.thread_manager)
        self.presence = PresenceService()
        self.approvals = KernelApprovalService()
        self.remote_task = RemoteTaskService(
            presence_service=self.presence, kernel_approval_service=self.approvals,
        )
        self.metrics = ChatRoomMetrics()
        self.conv = ChatConversationService(
            remote_task_service=self.remote_task, metrics=self.metrics,
        )
        self.coord = ChatTaskCoordinator(
            conversation_service=self.conv, remote_task_service=self.remote_task,
        )

    def open_thread(self, title="ai-collab"):
        async def go():
            return await self.chat.create_chat_thread(
                guild_id="g", parent_channel_id="p", title=title,
                topic=None, created_by="system",
            )
        return asyncio.run(go())

    def actor(self, name):
        return Actor(name, conv=self.conv, coord=self.coord)


def conv_state(cid: str) -> dict[str, Any]:
    with db_module.session_scope() as session:
        row = session.get(ChatConversationModel, cid)
        if row is None:
            return {}
        return {
            "kind": row.kind, "state": row.state, "resolution": row.resolution,
            "owner_actor": row.owner_actor, "expected_speaker": row.expected_speaker,
            "idle_warning_count": row.idle_warning_count or 0,
            "speech_count": row.speech_count or 0,
            "bound_task_id": row.bound_task_id, "closed_by": row.closed_by,
            "parent_conversation_id": row.parent_conversation_id,
        }


def count_kind(cid: str, event_kind: str) -> int:
    with db_module.session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == cid)
            .where(ChatMessageModel.event_kind == event_kind)
        ) or 0


# ---------------------------------------------------------------------------
# Scenarios


def scenario_01_prod_bug_to_hotfix(env, _t=None):
    """Prod /api/auth/login starts returning 500. alice reports it.
    claude-pca opens an inquiry to gather data. codex-pcb fishes the
    relevant log line. alice opens a hotfix task; claude-pca claims,
    runs the revert, evidences, completes."""
    section("01 — prod 500 bug -> claude triages, codex finds log, alice files hotfix task")
    thread = env.open_thread("incident-2026-05-01")
    alice = env.actor("alice")
    claude = env.actor("claude-pca")
    codex = env.actor("codex-pcb")

    # alice raises the issue in general (casual)
    env.chat.submit_participant_message(
        thread_id=thread.discord_thread_id, actor_name="alice", actor_kind="human",
        content="anyone seeing 500s on /api/auth/login?",
    )
    emit("alice", "speech.claim", '"anyone seeing 500s on /api/auth/login?"', "general")

    inquiry = claude.open_inquiry(
        thread, "Confirm 500 on /api/auth/login",
        intent="Need recent logs", addressed_to="codex-pcb",
    )
    codex.speak(inquiry.id, "evidence",
                "kubectl logs -n prod auth-7f4 -- 'NoneType has no attribute encode' x 142 hits/min",
                addressed_to="alice")
    codex.speak(inquiry.id, "claim",
                "stack points to commit 1a2b3c4 (PR #245) -- session_token serialization regression")
    claude.close(inquiry.id, "answered", "Root cause: PR #245 broke session token serializer")

    fix = alice.open_task(
        thread, "Hotfix: revert PR #245 in prod",
        objective="git revert 1a2b3c4 + redeploy auth service",
        parent_id=inquiry.id,
    )
    lease = claude.claim(fix.id, lease_seconds=300)
    claude.heartbeat(fix.id, lease_token=lease, phase="executing",
                     summary="cherry-picking revert", commands_run_count=1)
    claude.evidence(fix.id, lease_token=lease, kind="command_execution",
                    summary="git revert -n 1a2b3c4 -> revert-pr-245.patch (clean)",
                    payload={"exit_code": 0})
    claude.evidence(fix.id, lease_token=lease, kind="command_execution",
                    summary="kubectl rollout restart deployment/auth -n prod",
                    payload={"replicas": "3/3 ready"})
    claude.evidence(fix.id, lease_token=lease, kind="test_result",
                    summary="curl /api/auth/login -- 200 OK x 10/10",
                    payload={"passing": 10, "failing": 0})
    claude.complete(fix.id, lease_token=lease,
                    summary="prod 500 cleared; revert merged as PR #246")

    # asserts
    assert conv_state(inquiry.id)["resolution"] == "answered"
    assert conv_state(fix.id)["resolution"] == "completed"
    assert conv_state(fix.id)["parent_conversation_id"] == inquiry.id
    assert count_kind(fix.id, "chat.task.evidence") == 3


def scenario_02_build_test_deploy_pipeline(env, _t=None):
    """alice opens a deploy task. codex-pcb (build PC) builds, completes.
    claude-pca (test PC) opens follow-up task to run integration suite,
    completes. alice opens proposal "deploy yes/no"; both AIs agree;
    accepted."""
    section("02 — build -> test -> deploy pipeline across two PCs")
    thread = env.open_thread("release-v3.4.0")
    alice = env.actor("alice")
    codex = env.actor("codex-pcb")
    claude = env.actor("claude-pca")

    build = alice.open_task(
        thread, "Build v3.4.0 release artifacts",
        objective="build wheel + container image, tag as v3.4.0",
    )
    bl = codex.claim(build.id, lease_seconds=600)
    codex.heartbeat(build.id, lease_token=bl, phase="executing",
                    summary="pip wheel + docker build", commands_run_count=2)
    codex.evidence(build.id, lease_token=bl, kind="command_execution",
                   summary="docker build -t opscure:v3.4.0 . (12.4s, 412MB)",
                   payload={"image_id": "sha256:7c1a..."})
    codex.complete(build.id, lease_token=bl,
                   summary="opscure:v3.4.0 pushed to ghcr.io/semirain/opscure")

    smoke = claude.open_task(
        thread, "Smoke-test v3.4.0 image",
        objective="pull image and run integration suite against staging DB",
        parent_id=build.id,
    )
    sl = claude.claim(smoke.id, lease_seconds=600)
    claude.heartbeat(smoke.id, lease_token=sl, phase="executing",
                     summary="running pytest -m integration", tests_run_count=84)
    claude.evidence(smoke.id, lease_token=sl, kind="test_result",
                    summary="84 passed, 0 failed in 91.3s",
                    payload={"passing": 84, "failing": 0, "duration_s": 91.3})
    claude.complete(smoke.id, lease_token=sl, summary="all 84 integration tests green")

    decision = alice.open_proposal(
        thread, "Promote v3.4.0 to prod?",
        intent="build green + smoke green; merge window OK",
        owner="alice",
    )
    codex.speak(decision.id, "agree", "+1 -- image is reproducible, no infra deltas")
    claude.speak(decision.id, "agree", "+1 -- 0 test failures, no flaky")
    alice.close(decision.id, "accepted", "deployed at 14:22 UTC; rollback recipe: previous tag v3.3.7")

    assert conv_state(build.id)["resolution"] == "completed"
    assert conv_state(smoke.id)["resolution"] == "completed"
    assert conv_state(smoke.id)["parent_conversation_id"] == build.id
    assert conv_state(decision.id)["resolution"] == "accepted"


def scenario_03_code_review_with_objection(env, _t=None):
    """claude-pca finishes a refactor and asks for review. codex-pcb
    objects (N+1 queries). claude-pca opens a follow-up task to fix,
    completes, returns and closes original review proposal accepted."""
    section("03 — code review: codex objects (N+1), claude fixes, then merges")
    thread = env.open_thread("review-pr-302")
    alice = env.actor("alice")
    claude = env.actor("claude-pca")
    codex = env.actor("codex-pcb")

    refactor = claude.open_task(
        thread, "Refactor SessionService to async",
        objective="convert session_service.py to async + add pytest-asyncio markers",
    )
    rl = claude.claim(refactor.id, lease_seconds=600)
    claude.evidence(refactor.id, lease_token=rl, kind="file_write",
                    summary="rewrote session_service.py (412 -> 387 lines)",
                    payload={"files": ["nas_bridge/app/session_service.py"]})
    claude.evidence(refactor.id, lease_token=rl, kind="test_result",
                    summary="22 session tests passing",
                    payload={"passing": 22})
    claude.complete(refactor.id, lease_token=rl, summary="branch ready: refactor/session-async")

    review = claude.open_proposal(
        thread, "Merge PR #302 (refactor/session-async)?",
        intent="needs review before merge",
        owner="claude-pca",
    )
    codex.speak(review.id, "object",
                "session_service.list_active_sessions() now does 1+N selects -- "
                "missed the selectinload from the original sync path",
                addressed_to="claude-pca")

    fixup = claude.open_task(
        thread, "Add eager loading to fix N+1",
        objective="add selectinload(SessionModel.workers) to list_active_sessions",
        parent_id=review.id,
    )
    fl = claude.claim(fixup.id, lease_seconds=180)
    claude.evidence(fixup.id, lease_token=fl, kind="file_write",
                    summary="added selectinload + benchmark test asserting <=2 queries",
                    payload={"files": ["nas_bridge/app/session_service.py", "tests/test_session_query_count.py"]})
    claude.evidence(fixup.id, lease_token=fl, kind="test_result",
                    summary="benchmark: 2 queries for 50 sessions (was 51)",
                    payload={"queries": 2})
    claude.complete(fixup.id, lease_token=fl, summary="N+1 eliminated, push as PR #302 v2")

    codex.speak(review.id, "agree", "+1 -- 2 queries verified, ship it")
    claude.close(review.id, "accepted", "merged at HEAD~1; codex's catch saved a prod regression")

    assert conv_state(fixup.id)["parent_conversation_id"] == review.id
    assert conv_state(review.id)["resolution"] == "accepted"
    assert count_kind(review.id, "chat.speech.object") == 1
    assert count_kind(review.id, "chat.speech.agree") == 1


def scenario_04_hot_incident_response(env, _t=None):
    """bob reports prod outage. claude-pca opens task to find root cause,
    completes with finding. alice opens rollback task; codex-pcb
    executes. alice opens post-mortem proposal and hands off ownership."""
    section("04 — hot incident: claude finds cause, codex rolls back, post-mortem handed off")
    thread = env.open_thread("incident-2026-05-01-2")
    alice = env.actor("alice")
    bob = env.actor("bob")
    claude = env.actor("claude-pca")
    codex = env.actor("codex-pcb")

    env.chat.submit_participant_message(
        thread_id=thread.discord_thread_id, actor_name="bob", actor_kind="human",
        content="prod down -- /api/* all 503s starting ~3min ago",
    )
    emit("bob", "speech.claim", '"prod down -- /api/* all 503s ~3min ago"', "general")

    investigate = alice.open_task(
        thread, "Find root cause of /api/* 503s",
        objective="check pod status, recent deploys, db connections",
    )
    il = claude.claim(investigate.id, lease_seconds=300)
    claude.evidence(investigate.id, lease_token=il, kind="command_execution",
                    summary="kubectl get pods -n prod -- 4/8 auth pods CrashLoopBackOff",
                    payload={"crashing_pods": 4})
    claude.evidence(investigate.id, lease_token=il, kind="file_read",
                    summary="auth pod logs: 'too many connections' from db -- pool exhausted",
                    payload={"file": "/var/log/auth.log"})
    claude.evidence(investigate.id, lease_token=il, kind="command_execution",
                    summary="git log origin/main..HEAD~5 -- PR #245 increased pool_size from 20 to 200 (typo)",
                    payload={"culprit_pr": 245})
    claude.complete(investigate.id, lease_token=il,
                    summary="ROOT CAUSE: PR #245 set pool_size=200 (intent: 20); db hit max_connections")

    rollback = alice.open_task(
        thread, "Rollback PR #245 in prod",
        objective="git revert + emergency redeploy",
        parent_id=investigate.id,
    )
    rl = codex.claim(rollback.id, lease_seconds=300)
    codex.evidence(rollback.id, lease_token=rl, kind="command_execution",
                   summary="git revert PR #245 + kubectl set image deployment/auth opscure:v3.3.7",
                   payload={"reverted_to": "v3.3.7"})
    codex.evidence(rollback.id, lease_token=rl, kind="test_result",
                   summary="curl /api/health -- 200 OK; pods 8/8 ready in 47s",
                   payload={"recovery_seconds": 47})
    codex.complete(rollback.id, lease_token=rl, summary="prod healthy at 14:32 UTC; total downtime ~7min")

    postmortem = alice.open_proposal(
        thread, "Write post-mortem doc for 2026-05-01 outage",
        intent="standard 5-section post-mortem in docs/postmortems/",
        owner="alice",
    )
    alice.handoff(postmortem.id, new_owner="claude-pca",
                  reason="codex still has remaining deploy queue today")

    assert conv_state(investigate.id)["resolution"] == "completed"
    assert conv_state(rollback.id)["resolution"] == "completed"
    assert conv_state(postmortem.id)["owner_actor"] == "claude-pca"
    assert count_kind(postmortem.id, "chat.conversation.handoff") == 1


def scenario_05_knowledge_sharing_to_docs(env, _t=None):
    """A new dev (alice) asks how something works. claude-pca answers
    twice with code refs. alice proposes adding a runbook section.
    claude-pca opens a docs task, completes."""
    section("05 — knowledge sharing: alice asks about lease tokens, claude answers, alice files docs task")
    thread = env.open_thread("knowledge-base")
    alice = env.actor("alice")
    claude = env.actor("claude-pca")

    q1 = alice.open_inquiry(
        thread, "How does the lease token rotation work?",
        intent="reading remote_task_service for the first time",
        addressed_to="claude-pca",
    )
    claude.speak(q1.id, "answer",
                 "claim_task() issues lease_token via PresenceService.claim_resource_lease(); "
                 "heartbeat extends it; complete/fail releases.",
                 addressed_to="alice")
    claude.speak(q1.id, "evidence",
                 "see nas_bridge/app/services/remote_task_service.py:140-200 (claim) and 191-241 (heartbeat)")
    alice.close(q1.id, "answered", "got it; rotation is presence-service-owned")

    q2 = alice.open_inquiry(
        thread, "And what happens when the lease expires while a task is mid-execution?",
        addressed_to="claude-pca", parent_id=q1.id,
    )
    claude.speak(q2.id, "answer",
                 "the assignment row stays but lease_expires_at is in the past. "
                 "another claim() succeeds (presence sees no live holder); old worker's heartbeat "
                 "fails with InvalidLease. PR10 has the takeover test.",
                 addressed_to="alice")
    alice.close(q2.id, "answered", "perfect, will reference test_chat_conversation_concurrency.py")

    docs = alice.open_proposal(
        thread, "Add 'lease lifecycle' section to docs/architecture.md",
        intent="codify what claude just explained",
        owner="claude-pca", parent_id=q1.id,
    )
    docs_task = claude.open_task(
        thread, "Draft docs/architecture.md lease lifecycle section",
        objective="cover claim/heartbeat/expiration/recovery; reference PR10 test",
        parent_id=docs.id,
    )
    dl = claude.claim(docs_task.id, lease_seconds=900)
    claude.evidence(docs_task.id, lease_token=dl, kind="file_write",
                    summary="added 'Lease Lifecycle' section (47 lines) to docs/architecture.md")
    claude.complete(docs_task.id, lease_token=dl, summary="merged as PR #314")
    alice.close(docs.id, "accepted", "docs landing PR #314, thanks claude")

    assert conv_state(q1.id)["resolution"] == "answered"
    assert conv_state(q2.id)["parent_conversation_id"] == q1.id
    assert conv_state(docs_task.id)["resolution"] == "completed"
    assert conv_state(docs.id)["resolution"] == "accepted"


def scenario_06_three_concurrent_tasks(env, _t=None):
    """Three tasks open simultaneously. claude-pca on A, codex-pcb on B,
    alice on C (docs, herself). They check coordination via inquiry
    then proceed independently."""
    section("06 — three concurrent tasks in one room running independently")
    thread = env.open_thread("sprint-week-12")
    alice = env.actor("alice")
    claude = env.actor("claude-pca")
    codex = env.actor("codex-pcb")

    task_a = alice.open_task(thread, "Refactor PolicyService", objective="extract Policy from SessionService")
    task_b = alice.open_task(thread, "Add /api/health endpoint", objective="liveness + readiness probes")
    task_c = alice.open_task(thread, "Write release notes for v3.4.0", objective="generate from git log + PR titles")

    al = claude.claim(task_a.id, lease_seconds=900)
    bl = codex.claim(task_b.id, lease_seconds=300)
    cl = alice.claim(task_c.id, lease_seconds=180)

    coord = claude.open_inquiry(
        thread, "Need to coordinate refactor with /health endpoint?",
        addressed_to="codex-pcb",
    )
    codex.speak(coord.id, "answer",
                "no -- /health is in api/health.py, your refactor touches services/policy_service.py",
                addressed_to="claude-pca")
    claude.close(coord.id, "answered", "good, going parallel")

    claude.evidence(task_a.id, lease_token=al, kind="file_write", summary="created services/policy_service.py")
    codex.evidence(task_b.id, lease_token=bl, kind="file_write", summary="api/health.py with /healthz + /readyz")
    alice.evidence(task_c.id, lease_token=cl, kind="file_write", summary="docs/release-notes-v3.4.0.md")

    claude.complete(task_a.id, lease_token=al, summary="PolicyService extracted, all tests pass")
    codex.complete(task_b.id, lease_token=bl, summary="endpoints live; k8s probes wired")
    alice.complete(task_c.id, lease_token=cl, summary="release notes published")

    for tid in (task_a.id, task_b.id, task_c.id):
        assert conv_state(tid)["resolution"] == "completed"
    assert conv_state(coord.id)["resolution"] == "answered"


def scenario_07_lease_takeover_mid_task(env, _t=None):
    """claude-pca takes a long-running migration with a short lease.
    PC dies mid-execution; lease expires. codex-pcb comes online and
    takes over, completes."""
    section("07 — lease takeover when worker dies mid-task")
    thread = env.open_thread("migration-2026-q2")
    alice = env.actor("alice")
    claude = env.actor("claude-pca")
    codex = env.actor("codex-pcb")

    migrate = alice.open_task(
        thread, "Migrate users.last_active_at -> millisecond precision",
        objective="ALTER TABLE users + backfill ms timestamps from existing s timestamps",
    )
    cl = claude.claim(migrate.id, lease_seconds=30)
    claude.heartbeat(migrate.id, lease_token=cl, phase="executing",
                     summary="ALTER TABLE done; starting backfill", commands_run_count=1)
    claude.evidence(migrate.id, lease_token=cl, kind="command_execution",
                    summary="ALTER TABLE users ALTER COLUMN last_active_at TYPE bigint -- 0.4s")

    emit("system", "(simulated)", "claude-pca host loses power mid-backfill", short(migrate.id))
    expire_lease(migrate.bound_task_id)

    bl = codex.claim(migrate.id, lease_seconds=600)
    codex.evidence(migrate.id, lease_token=bl, kind="command_execution",
                   summary="resume backfill from last checkpoint (id > 142_000_000)",
                   payload={"resume_id": 142_000_000})
    codex.heartbeat(migrate.id, lease_token=bl, phase="executing",
                    summary="backfilling 38M rows", commands_run_count=1)
    codex.evidence(migrate.id, lease_token=bl, kind="command_execution",
                   summary="UPDATE users SET last_active_at = last_active_at * 1000 WHERE id > 142M -- done",
                   payload={"rows_updated": 38_000_000})
    codex.evidence(migrate.id, lease_token=bl, kind="test_result",
                   summary="row count + max(last_active_at) sanity checks pass")
    codex.complete(migrate.id, lease_token=bl,
                   summary="migration complete; took 22min total across 2 workers")

    state = conv_state(migrate.id)
    assert state["resolution"] == "completed"
    assert state["owner_actor"] == "codex-pcb"  # last claimant


def scenario_08_ai_defers_to_human_approval(env, _t=None):
    """claude-pca proposes a destructive cleanup; codex-pcb agrees but
    flags that it needs human approval. claude-pca hands off to alice,
    who approves with a guardrail."""
    section("08 — AI defers high-stakes decision to human approval")
    thread = env.open_thread("data-cleanup-2026-q2")
    alice = env.actor("alice")
    claude = env.actor("claude-pca")
    codex = env.actor("codex-pcb")

    proposal = claude.open_proposal(
        thread, "Delete user_events older than 90d",
        intent="table is 4.2TB; 90d is the retention policy in compliance doc v3",
        owner="claude-pca",
    )
    codex.speak(proposal.id, "agree",
                "agree on the goal -- but DELETE 4.2TB needs alice's signoff, not just AI consensus",
                addressed_to="claude-pca")
    claude.speak(proposal.id, "agree",
                "fair point; deferring to alice + adding audit log requirement", addressed_to="alice")
    claude.handoff(proposal.id, new_owner="alice",
                   reason="destructive op needs human sign-off per compliance v3")
    alice.speak(proposal.id, "claim",
                "approving with conditions: write audit row per delete batch + dry-run first")
    alice.close(proposal.id, "accepted",
                "approved with audit log + dry-run requirement; codex executes")

    state = conv_state(proposal.id)
    assert state["owner_actor"] == "alice"
    assert state["resolution"] == "accepted"
    assert state["closed_by"] == "alice"


def scenario_09_multi_day_async_with_idle(env, _t=None):
    """alice opens a proposal addressed to bob (offline). 60min idle ->
    tier-1 warning fires. bob comes back next day, but for the demo we
    backdate twice: tier-1 first, then again to simulate 4h+ -> tier-2.
    bob finally answers; alice closes accepted."""
    section("09 — multi-day async with idle escalation, then resumed")
    thread = env.open_thread("policy-2026-Q2")
    alice = env.actor("alice")
    bob = env.actor("bob")

    proposal = alice.open_proposal(
        thread, "Adopt 24h lease default (was 2h)",
        intent="long-running migrations were thrashing locks; want bob's read",
        owner="alice",
    )
    alice.speak(proposal.id, "claim", "@bob -- WDYT?", addressed_to="bob")

    backdate(proposal.id, minutes=35)
    flagged_t1 = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id, idle_threshold_seconds=30 * 60,
    )
    for f in flagged_t1:
        emit("system", "idle_warning", f"tier={f.idle_warning_count}", short(f.id))

    backdate(proposal.id, minutes=130)
    flagged_t2 = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id, idle_threshold_seconds=30 * 60,
    )
    for f in flagged_t2:
        emit("system", "idle_warning", f"tier={f.idle_warning_count}", short(f.id))

    # bob is back; the addressed-to slot still holds for him
    bob.speak(proposal.id, "answer",
              "sorry was at offsite. 24h is fine if migrations log progress every 30min; "
              "otherwise debugging a stuck task takes a day. condition: progress heartbeat required.",
              addressed_to="alice")
    alice.close(proposal.id, "accepted", "24h default + 30min heartbeat requirement; codex updates docs")

    state = conv_state(proposal.id)
    assert state["resolution"] == "accepted"
    assert state["idle_warning_count"] == 2  # tier-1 + tier-2 fired before bob answered
    assert count_kind(proposal.id, "chat.conversation.idle_warning") == 2


def scenario_10_brainstorm_consensus(env, _t=None):
    """alice opens a design proposal. claude-pca and codex-pcb both
    propose alternatives, debate, converge. alice closes with consensus
    summary."""
    section("10 — brainstorm: AIs converge on a consensus design")
    thread = env.open_thread("design-auth-v2")
    alice = env.actor("alice")
    claude = env.actor("claude-pca")
    codex = env.actor("codex-pcb")

    design = alice.open_proposal(
        thread, "Redesign auth -- pick a token format",
        intent="JWT vs PASETO vs opaque + redis lookup",
        owner="alice",
    )
    claude.speak(design.id, "propose",
                 "JWT with EdDSA: stateless verify, revocation list in redis. "
                 "good fit for our 50-pod horizontal scale.")
    codex.speak(design.id, "object",
                "JWT revocation list defeats statelessness; in practice you hit redis on every "
                "verify. why not just opaque tokens + redis from the start?",
                addressed_to="claude-pca")
    claude.speak(design.id, "agree",
                 "fair. opaque tokens give us simpler revocation. but PASETO v4 has built-in "
                 "expiry semantics that JWT lacks; would prefer that over raw opaque if we go stateful")
    codex.speak(design.id, "propose",
                "PASETO v4 + redis lookup is fine. it's stateful + has typed footers. "
                "agreed if we accept the redis dependency",
                addressed_to="alice")
    claude.speak(design.id, "agree", "+1 PASETO v4 + redis")
    alice.close(design.id, "accepted",
                "Decision: PASETO v4 tokens + redis-backed validation. "
                "claude drafts ADR; codex updates infra terraform for redis.")

    state = conv_state(design.id)
    assert state["resolution"] == "accepted"
    # 3 propose moves (claude, codex, codex), 2 agree, 1 object
    assert count_kind(design.id, "chat.speech.propose") == 2
    assert count_kind(design.id, "chat.speech.agree") == 2
    assert count_kind(design.id, "chat.speech.object") == 1


SCENARIOS: list[tuple[str, Callable[[Env, Any], None]]] = [
    ("01 prod 500 -> hotfix", scenario_01_prod_bug_to_hotfix),
    ("02 build -> test -> deploy", scenario_02_build_test_deploy_pipeline),
    ("03 code review with N+1 objection", scenario_03_code_review_with_objection),
    ("04 hot incident: cause / rollback / postmortem", scenario_04_hot_incident_response),
    ("05 knowledge -> docs", scenario_05_knowledge_sharing_to_docs),
    ("06 three concurrent tasks", scenario_06_three_concurrent_tasks),
    ("07 lease takeover mid-task", scenario_07_lease_takeover_mid_task),
    ("08 AI defers to human approval", scenario_08_ai_defers_to_human_approval),
    ("09 multi-day async + idle escalation", scenario_09_multi_day_async_with_idle),
    ("10 brainstorm consensus", scenario_10_brainstorm_consensus),
]


def analyze_conversation_log(env) -> None:
    """After all scenarios run, mine the persisted ChatMessageModel
    rows for collaboration dynamics. The transcripts above are
    human-readable; this pass surfaces the *shape* of the room:
    who-said-what, who-claimed-what, how decisions actually closed.

    All numbers are derived from the same SQLite the protocol writes
    to during the run -- no instrumentation, just queries."""
    section("Conversation log analysis")

    # Materialize everything to plain dicts inside the session scope
    # so we can iterate freely outside without DetachedInstanceError.
    with db_module.session_scope() as session:
        rooms = [
            {
                "id": r.id,
                "kind": r.kind,
                "title": r.title,
                "is_general": bool(r.is_general),
                "state": r.state,
                "resolution": r.resolution,
                "parent_conversation_id": r.parent_conversation_id,
                "owner_actor": r.owner_actor,
            }
            for r in session.scalars(
                select(ChatConversationModel)
                .order_by(ChatConversationModel.created_at.asc())
            )
        ]
        events = [
            {
                "actor_name": e.actor_name,
                "event_kind": e.event_kind,
                "conversation_id": e.conversation_id,
                "content": e.content,
            }
            for e in session.scalars(
                select(ChatMessageModel)
                .order_by(ChatMessageModel.created_at.asc())
            )
        ]

    print(f"  rooms (conversations): {len(rooms)}")
    print(f"  events (speech + lifecycle): {len(events)}")

    # ----- per-actor speech profile ---------------------------------------
    print()
    print("  per-actor speech profile (kind -> count):")
    by_actor: dict[str, dict[str, int]] = {}
    for ev in events:
        if not ev["event_kind"].startswith("chat.speech."):
            continue
        kind = ev["event_kind"][len("chat.speech."):]
        by_actor.setdefault(ev["actor_name"], {})[kind] = (
            by_actor.get(ev["actor_name"], {}).get(kind, 0) + 1
        )
    name_w = max((len(n) for n in by_actor), default=10)
    for actor in sorted(by_actor):
        kinds = by_actor[actor]
        total = sum(kinds.values())
        breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))
        print(f"    {actor:<{name_w}}  total={total:<3}  {breakdown}")

    # ----- per-actor protocol moves (open / close / handoff) --------------
    print()
    print("  per-actor protocol moves (open / close / handoff):")
    moves: dict[str, dict[str, int]] = {}
    for ev in events:
        if ev["event_kind"] == "chat.conversation.opened":
            moves.setdefault(ev["actor_name"], {})["open"] = moves.setdefault(ev["actor_name"], {}).get("open", 0) + 1
        elif ev["event_kind"] == "chat.conversation.closed":
            moves.setdefault(ev["actor_name"], {})["close"] = moves.setdefault(ev["actor_name"], {}).get("close", 0) + 1
        elif ev["event_kind"] == "chat.conversation.handoff":
            moves.setdefault(ev["actor_name"], {})["handoff"] = moves.setdefault(ev["actor_name"], {}).get("handoff", 0) + 1
    move_w = max((len(n) for n in moves), default=10)
    for actor in sorted(moves):
        m = moves[actor]
        print(f"    {actor:<{move_w}}  open={m.get('open', 0):<2} close={m.get('close', 0):<2} handoff={m.get('handoff', 0)}")

    # ----- per-actor task work --------------------------------------------
    print()
    print("  per-actor task work (claimed / completed / evidence rows):")
    task_work: dict[str, dict[str, int]] = {}
    for ev in events:
        if ev["event_kind"] == "chat.task.claimed":
            task_work.setdefault(ev["actor_name"], {})["claimed"] = task_work.setdefault(ev["actor_name"], {}).get("claimed", 0) + 1
        elif ev["event_kind"] == "chat.task.completed":
            task_work.setdefault(ev["actor_name"], {})["completed"] = task_work.setdefault(ev["actor_name"], {}).get("completed", 0) + 1
        elif ev["event_kind"] == "chat.task.evidence":
            task_work.setdefault(ev["actor_name"], {})["evidence"] = task_work.setdefault(ev["actor_name"], {}).get("evidence", 0) + 1
        elif ev["event_kind"] == "chat.task.heartbeat":
            task_work.setdefault(ev["actor_name"], {})["heartbeat"] = task_work.setdefault(ev["actor_name"], {}).get("heartbeat", 0) + 1
    work_w = max((len(n) for n in task_work), default=10)
    for actor in sorted(task_work):
        t = task_work[actor]
        print(
            f"    {actor:<{work_w}}  claimed={t.get('claimed', 0):<2} "
            f"completed={t.get('completed', 0):<2} "
            f"heartbeat={t.get('heartbeat', 0):<2} "
            f"evidence={t.get('evidence', 0)}"
        )

    # ----- conversation kind / resolution distribution --------------------
    print()
    print("  conversation kind x resolution matrix:")
    matrix: dict[str, dict[str, int]] = {}
    for room in rooms:
        if room["is_general"]:
            continue
        kind = room["kind"]
        res = room["resolution"] or "(open)"
        matrix.setdefault(kind, {})[res] = matrix.setdefault(kind, {}).get(res, 0) + 1
    for kind in sorted(matrix):
        items = ", ".join(f"{r}:{c}" for r, c in sorted(matrix[kind].items()))
        print(f"    {kind:<10}  {items}")

    # ----- cross-conversation parent_id linkage ---------------------------
    print()
    print("  cross-conversation linkage (parent_id graph):")
    children: dict[str, list[dict[str, Any]]] = {}
    for room in rooms:
        if room["parent_conversation_id"]:
            children.setdefault(room["parent_conversation_id"], []).append(room)
    if not children:
        print("    (no parent links)")
    else:
        rooms_by_id = {r["id"]: r for r in rooms}
        for parent_id, kids in children.items():
            parent = rooms_by_id.get(parent_id)
            parent_kind = parent["kind"] if parent else "?"
            parent_title = (parent["title"][:55] + "...") if parent and len(parent["title"]) > 55 else (parent["title"] if parent else "?")
            print(f'    [{parent_kind}] "{parent_title}"')
            for kid in kids:
                kid_title = (kid["title"][:50] + "...") if len(kid["title"]) > 50 else kid["title"]
                print(f"        -> [{kid['kind']}] \"{kid_title}\"  ({kid['resolution'] or 'open'})")

    # ----- multi-actor conversations (collab density) ---------------------
    print()
    print("  collaboration density (distinct speakers per non-general conversation):")
    by_conv_speakers: dict[str, set[str]] = {}
    for ev in events:
        if not ev["conversation_id"]:
            continue
        by_conv_speakers.setdefault(ev["conversation_id"], set()).add(ev["actor_name"])
    bucket = {1: 0, 2: 0, 3: 0, 4: 0}
    for room in rooms:
        if room["is_general"]:
            continue
        n = len(by_conv_speakers.get(room["id"], set()))
        bucket[min(n, 4)] = bucket.get(min(n, 4), 0) + 1
    for k in sorted(bucket):
        label = f"{k}+ speakers" if k == 4 else f"{k} speaker(s)"
        bar = "#" * bucket[k]
        print(f"    {label:<14} {bucket[k]:>2}  {bar}")

    # ----- idle activity --------------------------------------------------
    idle_events = [e for e in events if e["event_kind"] == "chat.conversation.idle_warning"]
    abandoned = [r for r in rooms if r["resolution"] == "abandoned"]
    print()
    print(f"  idle activity: {len(idle_events)} warnings emitted, {len(abandoned)} auto-abandoned")

    # ----- evidence kinds (what kinds of work AI agents posted) -----------
    print()
    print("  evidence kind distribution (what work was actually proven):")
    ev_kinds: dict[str, int] = {}
    for ev in events:
        if ev["event_kind"] != "chat.task.evidence":
            continue
        try:
            import json as _json
            payload = _json.loads(ev["content"])
            ek = payload.get("evidenceKind") or "(unknown)"
        except Exception:
            ek = "(unparseable)"
        ev_kinds[ek] = ev_kinds.get(ek, 0) + 1
    for k, v in sorted(ev_kinds.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<22} {v}")

    # ----- observation summary -------------------------------------------
    print()
    print("  observations:")
    if any(b > 1 for b in [v for k, v in bucket.items() if k >= 2]):
        print("    - multi-actor conversations dominate (collaborative, not soliloquy)")
    if matrix.get("task", {}).get("completed", 0) > 0 and matrix.get("task", {}).get("failed", 0) == 0:
        print(f"    - task success rate 100% ({matrix['task']['completed']}/{matrix['task']['completed']})")
    if children:
        max_chain = max(len(v) for v in children.values())
        print(f"    - parent->child linkage seen ({len(children)} parent rooms, max {max_chain} children)")
    if abandoned:
        print(f"    - {len(abandoned)} conversation(s) auto-abandoned by idle escalation")
    elif idle_events:
        print(f"    - {len(idle_events)} idle warning(s) fired but all conversations resumed before tier-3")


def main() -> int:
    db_module.init_db()
    env = Env()

    section("Boot")
    print(f"  tmp db = {os.environ['BRIDGE_DATABASE_URL']}")

    results: list[tuple[str, bool, str]] = []
    for name, fn in SCENARIOS:
        try:
            fn(env, None)
            results.append((name, True, ""))
        except AssertionError as exc:
            results.append((name, False, f"AssertionError: {exc}"))
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc().splitlines()[-3:]
            results.append((name, False, f"{type(exc).__name__}: {exc}  ({' / '.join(tb)})"))

    section("Summary")
    for name, ok, msg in results:
        status = "  ok " if ok else "FAIL"
        line = f"  [{status}] {name}"
        if not ok:
            line += f"  -- {msg}"
        print(line)
    pass_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - pass_count
    print()
    print(f"  total: {pass_count} passed, {fail_count} failed (of {len(results)})")
    print()
    print(f"  global metrics snapshot:")
    snap = env.metrics.snapshot()
    print(f"    conversations opened: {snap['conversations_opened']}")
    print(f"    closed by resolution: {snap['conversations_closed_by_resolution']}")
    print(f"    handoffs: {snap['handoffs']}")
    print(f"    speech by kind: {snap['speech_by_kind']}")
    print(f"    task lifecycle: {snap['task']}")
    print(f"    idle warnings by tier: {snap['idle_warnings_by_tier']}")

    analyze_conversation_log(env)

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
