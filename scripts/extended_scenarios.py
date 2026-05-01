"""Ten fresh scenarios exercising the PR13-PR21 capabilities.

Distinct from realistic_collab_scenarios.py (basic dev/ops happy
paths) and failure_mode_scenarios.py (adversarial). Each scenario
demonstrates ONE of the new capabilities -- identity binding,
approval flow, reply chain, bulk close, audit log, persistent
metrics, configurable thresholds, react+multi-address, read cursor
-- inside a realistic narrative.

Run:  python scripts/extended_scenarios.py

Scenarios:

  01 customer support tier escalation with approval gate
  02 sprint retrospective with metric snapshot + latency digest
  03 onboarding a new AI agent (identity wired + catch-up)
  04 merge-window code freeze (bulk close pending proposals)
  05 bug investigation tree (reply chain through findings)
  06 multi-AI design vote (react kind + multi-address)
  07 incident with rollback approval + interrupt
  08 incident room with compressed 5-min tier policy
  09 multi-day async with read-state evolution
  10 audit query: weekly "who did what" report
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

_TMP_DIR = tempfile.mkdtemp(prefix="opscure_extended_")
os.environ["BRIDGE_SHARED_AUTH_TOKEN"] = "demo-token"
os.environ["BRIDGE_DISABLE_DISCORD"] = "true"
os.environ["BRIDGE_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR, 'extended.db').as_posix()}"

for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

import app.config as _config  # noqa: E402
_config.get_settings.cache_clear()

import app.db as db_module  # noqa: E402
from app.behaviors.chat.conversation_schemas import (  # noqa: E402
    ChatTaskApprovalRequest,
    ChatTaskApprovalResolveRequest,
    ChatTaskClaimRequest,
    ChatTaskCompleteRequest,
    ChatTaskEvidenceRequest,
    ChatTaskHeartbeatRequest,
    ChatTaskInterruptRequest,
    ChatTaskNoteRequest,
    ConversationOpenRequest,
    SpeechActSubmitRequest,
)
from app.behaviors.chat.conversation_service import (  # noqa: E402
    ChatActorIdentityError,
    ChatConversationService,
    ChatPolicyConfig,
)
from app.behaviors.chat.metrics import ChatRoomMetrics  # noqa: E402
from app.behaviors.chat.models import ChatConversationModel, ChatMessageModel  # noqa: E402
from app.behaviors.chat.service import ChatBehaviorService  # noqa: E402
from app.behaviors.chat.task_coordinator import ChatTaskCoordinator  # noqa: E402
from app.kernel.approvals import KernelApprovalService  # noqa: E402
from app.kernel.presence import PresenceService  # noqa: E402
from app.services.remote_task_service import RemoteTaskService  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


# ---------------------------------------------------------------------------
# Harness


class StubThreadManager:
    def __init__(self) -> None:
        self.created_threads: list[str] = []

    async def create_thread(self, **kwargs):
        title = kwargs.get("title", "t")
        thread_id = f"discord-{title}-{len(self.created_threads) + 1}"
        self.created_threads.append(thread_id)
        return thread_id

    async def post_message(self, thread_id, content):
        return [("msg-stub", "")]


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def emit(actor: str, kind: str, detail: str, conv: str = "") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    prefix = f"[{conv}] " if conv else ""
    print(f"    {ts}  {actor:<14}  {kind:<28}  {prefix}{detail}")


def short(value: str | None) -> str:
    return (value or "")[:8]


def backdate(conversation_id: str, *, minutes: int) -> None:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with db_module.session_scope() as session:
        row = session.get(ChatConversationModel, conversation_id)
        if row is not None:
            row.created_at = moment
            row.last_speech_at = moment


class Env:
    def __init__(self, *, policy=None, actor_authorizer=None):
        self.thread_manager = StubThreadManager()
        self.chat = ChatBehaviorService(thread_manager=self.thread_manager)
        self.presence = PresenceService()
        self.approvals = KernelApprovalService()
        self.remote_task = RemoteTaskService(
            presence_service=self.presence, kernel_approval_service=self.approvals,
        )
        self.metrics = ChatRoomMetrics()
        self.conv = ChatConversationService(
            remote_task_service=self.remote_task,
            metrics=self.metrics,
            policy=policy,
            actor_authorizer=actor_authorizer,
        )
        self.coord = ChatTaskCoordinator(
            conversation_service=self.conv, remote_task_service=self.remote_task,
        )

    def open_thread(self, title="ext"):
        async def go():
            return await self.chat.create_chat_thread(
                guild_id="g", parent_channel_id="p", title=title,
                topic=None, created_by="system",
            )
        return asyncio.run(go())


# ---------------------------------------------------------------------------
# Scenarios


def s01_support_tier_escalation_with_approval(env=None):
    """Customer support: L1 -> L2 -> L3 escalation chain, with the
    final fix gated by a refund approval. Uses PR14 approval."""
    section("01 customer support tier escalation with approval gate")
    env = env or Env()
    thread = env.open_thread("support-T-1024")

    # L1: customer-bot triages incoming complaint
    triage = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="Ticket #1024: 'charged twice for one order'",
            opener_actor="support-l1-bot",
            intent="needs L2 if refund > $50",
        ),
    )
    emit("support-l1-bot", "triage", "double-charge complaint, $87.40", short(triage.id))
    env.conv.submit_speech(
        conversation_id=triage.id,
        request=SpeechActSubmitRequest(
            actor_name="support-l1-bot", kind="evidence",
            content="confirmed in stripe: charge_3Nx... and charge_3Ny..., 4s apart",
        ),
    )
    env.conv.submit_speech(
        conversation_id=triage.id,
        request=SpeechActSubmitRequest(
            actor_name="support-l1-bot", kind="defer",
            content="amount > L1 cap; escalating to L2",
            addressed_to="support-l2-bot",
        ),
    )

    # L2: opens a refund task and asks for L3 approval
    refund = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="Refund $87.40 to charge_3Ny...",
            opener_actor="support-l2-bot",
            objective="refund duplicate charge per ticket #1024",
            parent_conversation_id=triage.id,
        ),
    )
    response = env.coord.claim(
        conversation_id=refund.id,
        request=ChatTaskClaimRequest(actor_name="support-l2-bot", lease_seconds=300),
    )
    lease = response.task["current_assignment"]["lease_token"]
    env.coord.add_evidence(
        conversation_id=refund.id,
        request=ChatTaskEvidenceRequest(
            actor_name="support-l2-bot", lease_token=lease,
            kind="command_execution",
            summary="dry-run: stripe refund -d -a 8740 charge_3Ny...",
            payload={"would_refund_cents": 8740},
        ),
    )
    env.coord.request_approval(
        conversation_id=refund.id,
        request=ChatTaskApprovalRequest(
            actor_name="support-l2-bot", lease_token=lease,
            reason="$87.40 refund (>$50 L1 cap)",
            note="dry-run clean; awaiting L3 sign-off",
        ),
    )
    emit("support-l2-bot", "approval.requested", "$87.40 refund needs L3", short(refund.id))

    # L3: human / human-equivalent approves
    env.coord.resolve_approval(
        conversation_id=refund.id,
        request=ChatTaskApprovalResolveRequest(
            resolved_by="support-l3-human", resolution="approved",
            note="approved; tagged as ticket-1024-refund in audit",
        ),
    )
    env.coord.add_evidence(
        conversation_id=refund.id,
        request=ChatTaskEvidenceRequest(
            actor_name="support-l2-bot", lease_token=lease,
            kind="command_execution",
            summary="stripe refund -a 8740 charge_3Ny... -> re_3Nz...",
            payload={"refund_id": "re_3Nz..."},
        ),
    )
    env.coord.complete(
        conversation_id=refund.id,
        request=ChatTaskCompleteRequest(
            actor_name="support-l2-bot", lease_token=lease,
            summary="refund posted; customer will see in 3-5d",
        ),
    )
    env.conv.close_conversation(
        conversation_id=triage.id, closed_by="support-l1-bot",
        resolution="escalated", summary=f"resolved via refund task {short(refund.id)}",
    )

    # asserts
    with db_module.session_scope() as s:
        ref_row = s.get(ChatConversationModel, refund.id)
        assert ref_row.resolution == "completed"
        triage_row = s.get(ChatConversationModel, triage.id)
        assert triage_row.resolution == "escalated"
        approvals = s.scalar(
            select(func.count())
            .select_from(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == refund.id)
            .where(ChatMessageModel.event_kind == "chat.task.approval_resolved")
        ) or 0
        assert approvals == 1


def s02_sprint_retro_with_metric_digest(env=None):
    """Sprint retro: open 3 inquiries to surface lessons, capture a
    metric snapshot at the end, query latency stats. PR17."""
    section("02 sprint retrospective with metric snapshot + latency digest")
    env = env or Env()
    thread = env.open_thread("sprint-week-14-retro")

    inq_a = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="What went well?",
            opener_actor="alice",
        ),
    )
    inq_b = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="What slowed us down?",
            opener_actor="alice",
        ),
    )
    inq_c = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="What to try next sprint?",
            opener_actor="alice",
        ),
    )
    for cid, content in [
        (inq_a.id, "claude-pca: kernel Operation promotion landed clean"),
        (inq_a.id, "codex-pcb: PR review turnaround dropped to 4h avg"),
        (inq_b.id, "claude-pca: pre-existing test failures sat for 3 sprints"),
        (inq_b.id, "codex-pcb: 2 incidents traced to lack of approval gate"),
        (inq_c.id, "alice: try ChatPolicyConfig per room kind"),
    ]:
        author = content.split(":")[0]
        env.conv.submit_speech(
            conversation_id=cid,
            request=SpeechActSubmitRequest(
                actor_name=author, kind="claim",
                content=content.split(":", 1)[1].strip(),
            ),
        )
    # Backdate the conversations to fake elapsed sprint time
    backdate(inq_a.id, minutes=180)
    backdate(inq_b.id, minutes=240)
    backdate(inq_c.id, minutes=120)

    env.conv.close_conversation(conversation_id=inq_a.id, closed_by="alice", resolution="answered")
    env.conv.close_conversation(conversation_id=inq_b.id, closed_by="alice", resolution="answered")
    env.conv.close_conversation(conversation_id=inq_c.id, closed_by="alice", resolution="answered")

    snap = env.conv.capture_metric_snapshot(discord_thread_id=thread.discord_thread_id)
    emit("alice", "metric.snapshot", f"id={short(snap['id'])} closed={snap['snapshot']['conversations_closed_by_resolution']}", short(thread.id))

    stats = env.conv.compute_latency_stats(discord_thread_id=thread.discord_thread_id)
    emit("alice", "latency.stats",
         f"sample={stats['sample_size']} avg={int(stats['overall']['avg'])}s",
         short(thread.id))

    history = env.conv.get_metric_history(discord_thread_id=thread.discord_thread_id)
    assert len(history) == 1
    assert stats["sample_size"] == 3
    assert "inquiry" in stats["by_kind"]
    # avg should be in the 2-4h band given backdates 120/180/240min
    assert 100 * 60 < stats["overall"]["avg"] < 250 * 60


def s03_onboarding_new_ai_with_identity_and_catchup(env=None):
    """A new AI 'gemini-pcc' joins. Identity authorizer is wired so
    only authorized tokens may speak as gemini-pcc. The new agent
    catches up by reading the room (mark_conversation_read) before
    contributing. PR13 + PR21."""
    section("03 onboarding new AI agent: identity wired + catch-up via mark-read")

    # token-aware authorizer: each actor name is bound to one token
    def token_authorizer(caller_ctx, actor_name):
        return caller_ctx == f"{actor_name}-token"

    env = Env(actor_authorizer=token_authorizer)
    thread = env.open_thread("welcome-gemini")

    # Existing room activity (pre-existing conversation gemini wasn't part of)
    design = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="proposal", title="Pick a vector DB for RAG",
            opener_actor="alice", intent="qdrant vs pgvector vs weaviate",
        ),
        caller_context="alice-token",
    )
    env.conv.submit_speech(
        conversation_id=design.id,
        request=SpeechActSubmitRequest(
            actor_name="claude-pca", kind="propose",
            content="qdrant: best filter perf at ~10M vectors",
        ),
        caller_context="claude-pca-token",
    )
    env.conv.submit_speech(
        conversation_id=design.id,
        request=SpeechActSubmitRequest(
            actor_name="codex-pcb", kind="object",
            content="adds operational dependency; pgvector keeps us inside postgres",
        ),
        caller_context="codex-pcb-token",
    )

    # gemini joins -- catches up to current state before contributing
    initial_unread = env.conv.get_conversation_read_status(
        conversation_id=design.id, actor_name="gemini-pcc",
    )
    emit("gemini-pcc", "joins", f"unread={initial_unread['unread_count']}", short(design.id))
    env.conv.mark_conversation_read(
        conversation_id=design.id, actor_name="gemini-pcc",
        caller_context="gemini-pcc-token",
    )
    after_catchup = env.conv.get_conversation_read_status(
        conversation_id=design.id, actor_name="gemini-pcc",
    )
    emit("gemini-pcc", "catchup", f"unread={after_catchup['unread_count']}", short(design.id))

    # Now gemini contributes -- with proper token, accepted
    env.conv.submit_speech(
        conversation_id=design.id,
        request=SpeechActSubmitRequest(
            actor_name="gemini-pcc", kind="propose",
            content="weaviate has hybrid (BM25 + vector) baked in; "
                    "saves us building the rerank stage",
        ),
        caller_context="gemini-pcc-token",
    )

    # Identity: claude tries to speak as gemini -- rejected
    rejected = False
    try:
        env.conv.submit_speech(
            conversation_id=design.id,
            request=SpeechActSubmitRequest(
                actor_name="gemini-pcc", kind="agree", content="(claude impersonating)",
            ),
            caller_context="claude-pca-token",
        )
    except ChatActorIdentityError:
        rejected = True
    emit("claude-pca", "(impersonation)", "tried to speak as gemini-pcc", short(design.id))

    assert initial_unread["unread_count"] >= 3  # opened + 2 speeches at least
    assert after_catchup["unread_count"] == 0
    assert rejected is True


def s04_merge_window_code_freeze(env=None):
    """Friday afternoon: code freeze starts. operator bulk-closes
    all 'open' proposals on the release thread as 'withdrawn' to
    ensure no surprise merges over the weekend. PR16 bulk close."""
    section("04 merge-window code freeze (bulk close pending proposals)")
    env = env or Env()
    thread = env.open_thread("release-v3.5.0-window")

    # 4 proposals open; 1 task in flight (must NOT be touched)
    open_proposals = []
    for i, title in enumerate([
        "Bump python to 3.13",
        "Migrate to pydantic v3",
        "Switch CI to ubuntu-24.04",
        "Drop deprecated /api/v1/legacy",
    ], 1):
        p = env.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="proposal", title=title, opener_actor="alice",
            ),
        )
        open_proposals.append(p.id)

    active_task = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="Cherry-pick critical security fix",
            opener_actor="alice", objective="backport CVE-2026-1234 fix",
        ),
    )
    env.coord.claim(
        conversation_id=active_task.id,
        request=ChatTaskClaimRequest(actor_name="codex-pcb", lease_seconds=600),
    )

    # operator triggers the freeze: bulk close proposals only
    snap = env.conv.bulk_close_conversations(
        conversation_ids=open_proposals,
        closed_by="alice", resolution="withdrawn",
        summary="auto-closed for v3.5.0 code freeze; reopen Monday",
    )
    emit("alice", "bulk.close", f"requested={snap['requested']} succeeded={snap['succeeded']}", short(thread.id))

    # task untouched
    with db_module.session_scope() as s:
        task_row = s.get(ChatConversationModel, active_task.id)
        assert task_row.state == "open"
        for pid in open_proposals:
            row = s.get(ChatConversationModel, pid)
            assert row.resolution == "withdrawn"
    assert snap["succeeded"] == 4
    assert snap["failed"] == 0


def s05_bug_investigation_with_reply_chain(env=None):
    """Long-running bug investigation: claude opens an inquiry, codex
    posts findings, claude replies to a specific finding with a
    follow-up theory, gemini chimes in with a counter-hypothesis. The
    reply tree shows the investigation path. PR15."""
    section("05 bug investigation tree (reply chain through findings)")
    env = env or Env()
    thread = env.open_thread("bug-2026-1024-flaky-tests")

    inquiry = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="Why is test_lease_takeover flaky on CI?",
            opener_actor="claude-pca", intent="passes locally, fails 1-in-10 on GHA",
        ),
    )
    f1 = env.conv.submit_speech(
        conversation_id=inquiry.id,
        request=SpeechActSubmitRequest(
            actor_name="codex-pcb", kind="evidence",
            content="grepped 100 runs -- failure correlates with parallel job count >=4",
        ),
    )
    f2 = env.conv.submit_speech(
        conversation_id=inquiry.id,
        request=SpeechActSubmitRequest(
            actor_name="codex-pcb", kind="evidence",
            content="strace of failing run shows fsync contention on /tmp",
        ),
    )
    # claude replies specifically to f1 with a theory
    t1 = env.conv.submit_speech(
        conversation_id=inquiry.id,
        request=SpeechActSubmitRequest(
            actor_name="claude-pca", kind="claim",
            content="theory: short lease (30s) + slow tmpdir = lease expires "
                    "before assignment row commits",
            replies_to_speech_id=f1.id,
        ),
    )
    # gemini counter-hypothesis, replying to f2
    t2 = env.conv.submit_speech(
        conversation_id=inquiry.id,
        request=SpeechActSubmitRequest(
            actor_name="gemini-pcc", kind="object",
            content="not fsync -- look at the tmp_path fixture; it's per-test, "
                    "not per-worker. parallel runs could collide",
            replies_to_speech_id=f2.id,
        ),
    )
    # claude agrees with gemini, replying to t2
    a1 = env.conv.submit_speech(
        conversation_id=inquiry.id,
        request=SpeechActSubmitRequest(
            actor_name="claude-pca", kind="agree",
            content="checked -- tmp_path isn't worker-scoped here. that's the bug.",
            replies_to_speech_id=t2.id,
        ),
    )
    env.conv.close_conversation(
        conversation_id=inquiry.id, closed_by="claude-pca",
        resolution="answered",
        summary="root cause: shared tmp_path; fix in PR #401",
    )

    detail = env.conv.get_conversation(conversation_id=inquiry.id, recent=20)
    by_id = {s.id: s for s in detail.recent_speech}
    # verify the reply tree
    assert by_id[t1.id].replies_to_speech_id == f1.id
    assert by_id[t2.id].replies_to_speech_id == f2.id
    assert by_id[a1.id].replies_to_speech_id == t2.id


def s06_design_vote_with_react_and_multi_address(env=None):
    """A design proposal goes to vote across 3 AIs + alice. Each AI
    casts a 'react' for a quick +1 instead of a full agree speech.
    The proposal is multi-addressed so all reviewers see it as
    expected. PR20 react + multi-address."""
    section("06 multi-AI design vote (react kind + multi-address)")
    env = env or Env()
    thread = env.open_thread("design-rfc-2026-006")

    proposal = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="proposal", title="RFC-006: switch to JWT-EdDSA tokens",
            opener_actor="alice",
            intent="needs majority vote from infra reviewers",
        ),
    )
    # alice multi-addresses the request
    env.conv.submit_speech(
        conversation_id=proposal.id,
        request=SpeechActSubmitRequest(
            actor_name="alice", kind="propose",
            content="proposing JWT EdDSA + 5min revocation cache. Vote please.",
            addressed_to="claude-pca",
            addressed_to_many=["claude-pca", "codex-pcb", "gemini-pcc"],
        ),
    )

    # Each reviewer reacts (+1) instead of writing a full agree
    for actor in ("claude-pca", "codex-pcb", "gemini-pcc"):
        env.conv.submit_speech(
            conversation_id=proposal.id,
            request=SpeechActSubmitRequest(
                actor_name=actor, kind="react", content="+1",
            ),
        )
        emit(actor, "speech.react", "+1", short(proposal.id))

    env.conv.close_conversation(
        conversation_id=proposal.id, closed_by="alice", resolution="accepted",
        summary="3/3 reviewer +1; landing as PR #432",
    )

    detail = env.conv.get_conversation(conversation_id=proposal.id)
    react_count = sum(1 for s in detail.recent_speech if s.kind == "react")
    multi_addr_speech = next(
        (s for s in detail.recent_speech if s.addressed_to_many and len(s.addressed_to_many) >= 3),
        None,
    )
    assert react_count == 3
    assert multi_addr_speech is not None
    assert "gemini-pcc" in multi_addr_speech.addressed_to_many


def s07_incident_with_rollback_approval_and_interrupt(env=None):
    """Prod incident: codex starts a rollback task. Mid-execution,
    alice realizes the rollback would corrupt state and INTERRUPTS.
    A new task is opened with a different approach, requiring
    approval before destructive ops. PR14 approval + interrupt."""
    section("07 incident with rollback approval + mid-task interrupt")
    env = env or Env()
    thread = env.open_thread("incident-2026-05-02-prod")

    rollback = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="Rollback PR #501 from prod",
            opener_actor="alice", objective="git revert + redeploy",
        ),
    )
    response = env.coord.claim(
        conversation_id=rollback.id,
        request=ChatTaskClaimRequest(actor_name="codex-pcb", lease_seconds=300),
    )
    lease = response.task["current_assignment"]["lease_token"]
    env.coord.add_evidence(
        conversation_id=rollback.id,
        request=ChatTaskEvidenceRequest(
            actor_name="codex-pcb", lease_token=lease,
            kind="command_execution", summary="git revert -n abc123",
        ),
    )
    env.coord.add_evidence(
        conversation_id=rollback.id,
        request=ChatTaskEvidenceRequest(
            actor_name="codex-pcb", lease_token=lease,
            kind="command_execution",
            summary="kubectl rollout undo deployment/api -- in progress",
        ),
    )

    # alice realizes the rollback would corrupt state -- interrupt
    env.coord.interrupt(
        conversation_id=rollback.id,
        request=ChatTaskInterruptRequest(
            actor_name="codex-pcb", lease_token=lease,
            note="alice flagged: rollback would orphan in-flight payments. "
                 "switching to forward-fix approach.",
        ),
    )
    emit("alice", "interrupt", "rollback would corrupt payment state", short(rollback.id))

    # forward-fix task opened; approval required before destructive op
    forward = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="Forward-fix: patch payment idempotency check",
            opener_actor="alice",
            objective="add idempotency_key check; deploy hotfix",
            parent_conversation_id=rollback.id,
        ),
    )
    fresp = env.coord.claim(
        conversation_id=forward.id,
        request=ChatTaskClaimRequest(actor_name="codex-pcb", lease_seconds=600),
    )
    flease = fresp.task["current_assignment"]["lease_token"]
    env.coord.add_evidence(
        conversation_id=forward.id,
        request=ChatTaskEvidenceRequest(
            actor_name="codex-pcb", lease_token=flease,
            kind="file_write",
            summary="payment_service.py: added idempotency_key validation",
        ),
    )
    env.coord.request_approval(
        conversation_id=forward.id,
        request=ChatTaskApprovalRequest(
            actor_name="codex-pcb", lease_token=flease,
            reason="prod hotfix; needs human sign-off before kubectl apply",
            note="diff reviewed; ready to deploy",
        ),
    )
    env.coord.resolve_approval(
        conversation_id=forward.id,
        request=ChatTaskApprovalResolveRequest(
            resolved_by="alice", resolution="approved",
            note="approved; tagged in incident log",
        ),
    )
    env.coord.add_evidence(
        conversation_id=forward.id,
        request=ChatTaskEvidenceRequest(
            actor_name="codex-pcb", lease_token=flease,
            kind="command_execution",
            summary="kubectl apply -f payment-hotfix.yaml -- 3/3 ready",
        ),
    )
    env.coord.complete(
        conversation_id=forward.id,
        request=ChatTaskCompleteRequest(
            actor_name="codex-pcb", lease_token=flease,
            summary="hotfix live; payment idempotency restored",
        ),
    )

    with db_module.session_scope() as s:
        rb = s.get(ChatConversationModel, rollback.id)
        fw = s.get(ChatConversationModel, forward.id)
        assert rb.state == "open"  # interrupt left it open
        assert fw.resolution == "completed"
        assert fw.parent_conversation_id == rollback.id


def s08_incident_room_with_compressed_tier_policy(env=None):
    """An incident-only room uses ChatPolicyConfig with much tighter
    tier multipliers (1/2/4 instead of 1/4/48). A 5-min idle on an
    open task fires tier-1 immediately. PR19."""
    section("08 incident room with compressed 5-min tier policy")
    incident_policy = ChatPolicyConfig(
        tier_1_multiplier=1, tier_2_multiplier=2, tier_3_multiplier=4,
        over_speech_threshold=3,
    )
    env = Env(policy=incident_policy)
    thread = env.open_thread("oncall-incident-room")

    flag = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="proposal", title="Page on-call for db-replica-2 lag?",
            opener_actor="oncall-bot", addressed_to="oncall-human",
        ),
    )
    # backdate by 6min -- with tier-1 = 5min base, tier-1 fires
    backdate(flag.id, minutes=6)
    flagged = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=5 * 60,
    )
    emit("system", "tier-1 warning",
         f"after 6min silence (incident policy)", short(flag.id))
    assert len(flagged) == 1
    assert flagged[0].idle_warning_count == 1

    # backdate further to 25min -- with tier-3 = 4x = 20min, auto-abandon
    backdate(flag.id, minutes=25)
    flagged2 = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=5 * 60,
    )
    abandoned = [f for f in flagged2 if f.resolution == "abandoned"]
    emit("system", "auto-abandoned",
         f"after 25min silence (would be normal-room tier-1)",
         short(flag.id))
    assert len(abandoned) == 1


def s09_multi_day_async_with_read_evolution(env=None):
    """A long-running design discussion spans multiple days. bob is
    on PTO for day 1, returns day 2, catches up via mark-read, then
    contributes. The read cursor evolves across the timeline. PR21."""
    section("09 multi-day async with read-state evolution")
    env = env or Env()
    thread = env.open_thread("design-monorepo-split")

    proposal = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="proposal", title="Split nas_bridge into 2 packages?",
            opener_actor="alice", owner="alice",
        ),
    )
    # day 1: alice + claude discuss
    for content in [
        ("alice", "I'm thinking pkg-core (kernel+events) + pkg-product (behaviors)"),
        ("claude-pca", "import boundaries are clean; I count ~12 cross-pkg refs"),
        ("alice", "any blockers?"),
        ("claude-pca", "tests would split too -- need parallel CI matrix"),
        ("alice", "fair. let's wait on bob's input."),
    ]:
        env.conv.submit_speech(
            conversation_id=proposal.id,
            request=SpeechActSubmitRequest(
                actor_name=content[0], kind="claim", content=content[1],
            ),
        )

    # End of day 1 -- alice + claude check out (mark read up to here)
    env.conv.mark_conversation_read(conversation_id=proposal.id, actor_name="alice")
    env.conv.mark_conversation_read(conversation_id=proposal.id, actor_name="claude-pca")

    # End of day 1 -- bob comes back
    bob_initial = env.conv.get_conversation_read_status(
        conversation_id=proposal.id, actor_name="bob",
    )
    emit("bob", "joins day-2", f"unread={bob_initial['unread_count']}", short(proposal.id))

    # Bob catches up
    env.conv.mark_conversation_read(
        conversation_id=proposal.id, actor_name="bob",
    )
    bob_after_catchup = env.conv.get_conversation_read_status(
        conversation_id=proposal.id, actor_name="bob",
    )
    emit("bob", "caught up", f"unread={bob_after_catchup['unread_count']}", short(proposal.id))

    # Day 2: bob contributes; alice + claude both increment unread for OTHERS
    env.conv.submit_speech(
        conversation_id=proposal.id,
        request=SpeechActSubmitRequest(
            actor_name="bob", kind="agree",
            content="agree on the split. infra concern: deploy order matters now.",
        ),
    )
    env.conv.submit_speech(
        conversation_id=proposal.id,
        request=SpeechActSubmitRequest(
            actor_name="bob", kind="propose",
            content="introduce pkg-bridge as a thin facade so external repos "
                    "don't break on the split",
        ),
    )

    # alice/claude now have 2 unread
    alice_status = env.conv.get_conversation_read_status(
        conversation_id=proposal.id, actor_name="alice",
    )
    claude_status = env.conv.get_conversation_read_status(
        conversation_id=proposal.id, actor_name="claude-pca",
    )
    emit("alice", "next-day return", f"unread={alice_status['unread_count']}", short(proposal.id))
    emit("claude-pca", "next-day return", f"unread={claude_status['unread_count']}", short(proposal.id))

    assert bob_initial["unread_count"] >= 6
    assert bob_after_catchup["unread_count"] == 0
    assert alice_status["unread_count"] == 2  # bob's 2 messages
    assert claude_status["unread_count"] == 2


def s10_weekly_audit_who_did_what(env=None):
    """End-of-week operator query: 'show me what claude-pca did this
    week in the support thread.' Use audit log search to assemble
    the digest. PR16."""
    section("10 weekly audit: who did what report")
    env = env or Env()
    thread = env.open_thread("ops-weekly-2026-w18")

    # Generate a week of activity
    for day in range(5):
        inq = env.conv.open_conversation(
            discord_thread_id=thread.discord_thread_id,
            request=ConversationOpenRequest(
                kind="inquiry", title=f"day-{day} question",
                opener_actor="alice",
            ),
        )
        if day % 2 == 0:
            env.conv.submit_speech(
                conversation_id=inq.id,
                request=SpeechActSubmitRequest(
                    actor_name="claude-pca", kind="answer",
                    content=f"day-{day} answer",
                ),
            )
        else:
            env.conv.submit_speech(
                conversation_id=inq.id,
                request=SpeechActSubmitRequest(
                    actor_name="codex-pcb", kind="answer",
                    content=f"day-{day} answer",
                ),
            )
        env.conv.close_conversation(
            conversation_id=inq.id, closed_by="alice", resolution="answered",
        )

    # Operator query: claude's activity on this thread
    claude_log = env.conv.search_audit_log(
        thread_id=thread.discord_thread_id, actor_name="claude-pca",
    )
    codex_log = env.conv.search_audit_log(
        thread_id=thread.discord_thread_id, actor_name="codex-pcb",
    )
    emit("alice", "audit.report",
         f"claude={len(claude_log['items'])} codex={len(codex_log['items'])}",
         short(thread.id))

    # only answer kinds
    answer_log = env.conv.search_audit_log(
        thread_id=thread.discord_thread_id, event_kind="chat.speech.answer",
    )
    emit("alice", "audit.answers",
         f"total answers: {len(answer_log['items'])}",
         short(thread.id))
    assert len(answer_log["items"]) == 5
    # claude answered on days 0, 2, 4 -- 3 events
    assert len(claude_log["items"]) == 3
    # codex answered on days 1, 3 -- 2 events
    assert len(codex_log["items"]) == 2


# ---------------------------------------------------------------------------


SCENARIOS: list[tuple[str, Callable[..., None]]] = [
    ("01 support tier escalation w/ approval", s01_support_tier_escalation_with_approval),
    ("02 sprint retro + metric digest", s02_sprint_retro_with_metric_digest),
    ("03 onboarding: identity + catch-up", s03_onboarding_new_ai_with_identity_and_catchup),
    ("04 merge-window code freeze (bulk close)", s04_merge_window_code_freeze),
    ("05 bug investigation with reply chain", s05_bug_investigation_with_reply_chain),
    ("06 design vote: react + multi-address", s06_design_vote_with_react_and_multi_address),
    ("07 incident: rollback approval + interrupt", s07_incident_with_rollback_approval_and_interrupt),
    ("08 incident room: compressed tier policy", s08_incident_room_with_compressed_tier_policy),
    ("09 multi-day async w/ read-state evolution", s09_multi_day_async_with_read_evolution),
    ("10 weekly audit: who did what", s10_weekly_audit_who_did_what),
]


def main() -> int:
    db_module.init_db()

    section("Boot")
    print(f"  tmp db = {os.environ['BRIDGE_DATABASE_URL']}")

    results: list[tuple[str, bool, str]] = []
    for name, fn in SCENARIOS:
        try:
            fn()
            results.append((name, True, ""))
        except AssertionError as exc:
            results.append((name, False, f"AssertionError: {exc}"))
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc().splitlines()[-3:]
            results.append((name, False, f"{type(exc).__name__}: {exc}  ({' / '.join(tb)})"))

    section("Summary")
    pass_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - pass_count
    for name, ok, msg in results:
        status = "  ok " if ok else "FAIL"
        line = f"  [{status}] {name}"
        if not ok:
            line += f"  -- {msg[:120]}"
        print(line)
    print()
    print(f"  total: {pass_count} passed, {fail_count} failed (of {len(results)})")
    print()
    print("  features exercised across these 10 scenarios:")
    print("    PR13 actor identity binding         -> #03")
    print("    PR14 approval / interrupt flow      -> #01, #07")
    print("    PR15 reply chain                    -> #05")
    print("    PR16 bulk close + audit log         -> #04, #10")
    print("    PR17 persistent metrics + latency   -> #02")
    print("    PR19 ChatPolicyConfig (tunable tiers) -> #08")
    print("    PR20 react + multi-address          -> #06")
    print("    PR21 per-actor read cursor          -> #03, #09")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
