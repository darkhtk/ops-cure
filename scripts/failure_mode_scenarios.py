"""Ten unhappy-path scenarios -- adversarial actors, broken sequences,
abuse patterns. Pairs with realistic_collab_scenarios.py (happy paths)
to map the protocol's actual defensive surface.

Each scenario is labeled PROTECTED (system rejects/handles) or GAP
(system accepts when it arguably shouldn't). The final analysis pass
totals coverage so the protocol's blind spots are visible.

Run:  python scripts/failure_mode_scenarios.py

Why GAP scenarios exist:
    A "GAP" is intentionally documented, not silently failing. The
    test asserts the *current* behavior so a future hardening change
    breaks the test loudly and someone has to consciously update
    both the protocol and the assertion. That's the difference
    between "we forgot" and "we know but haven't shipped a fix yet".

Scenarios:

  01 PROTECTED  lease_token forgery on heartbeat
  02 PROTECTED  resolution-vs-kind enum mismatch on close
  03 PROTECTED  speech submission to a closed conversation
  04 PROTECTED  unauthorized actor tries to close
  05 PROTECTED  tier-3 auto-abandon under sustained silence
  06 GAP        actor-name spoofing in speech (no identity binding)
  07 GAP        evidence injection by non-lease-holder
  08 GAP        task complete without any heartbeat or evidence
  09 GAP        evidence with arbitrary/fake kind string
  10 GAP        loop conversation that never converges (idle won't fire)
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

_TMP_DIR = tempfile.mkdtemp(prefix="opscure_failures_")
os.environ["BRIDGE_SHARED_AUTH_TOKEN"] = "demo-token"
os.environ["BRIDGE_DISABLE_DISCORD"] = "true"
os.environ["BRIDGE_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR, 'failures.db').as_posix()}"

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
from app.behaviors.chat.conversation_service import ChatConversationService, ChatConversationStateError  # noqa: E402
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


def emit(actor: str, kind: str, detail: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"    {ts}  {actor:<13}  {kind:<28}  {detail}")


def backdate(conversation_id: str, *, minutes: int) -> None:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with db_module.session_scope() as session:
        row = session.get(ChatConversationModel, conversation_id)
        if row is not None:
            row.created_at = moment
            row.last_speech_at = moment


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

    def open_thread(self, title="failure-mode"):
        async def go():
            return await self.chat.create_chat_thread(
                guild_id="g", parent_channel_id="p", title=title,
                topic=None, created_by="system",
            )
        return asyncio.run(go())


# Result tracking: each scenario reports whether the protocol behaved
# as protected (rejected the abuse) or showed a gap (accepted it).
RESULT_PROTECTED = "PROTECTED"
RESULT_GAP = "GAP"
ScenarioResult = dict[str, Any]
_results: list[ScenarioResult] = []


def report(label: str, kind: str, detail: str) -> None:
    """Record a scenario outcome. ``kind`` is RESULT_PROTECTED or
    RESULT_GAP; ``detail`` describes what actually happened."""
    _results.append({"label": label, "kind": kind, "detail": detail})
    marker = "[OK]" if kind == RESULT_PROTECTED else "[!!]"
    emit("analysis", f"{marker} {kind}", detail)


# ---------------------------------------------------------------------------
# Scenarios


def s01_lease_forgery_on_heartbeat(env):
    """PROTECTED. AI A claims a task. AI B forges a lease_token and
    tries to heartbeat. RemoteTaskService rejects via lease check."""
    section("01 PROTECTED: lease forgery on heartbeat")
    thread = env.open_thread("s01")
    task_conv = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="x",
        ),
    )
    cid = task_conv.id

    # actor A claims, gets a real token
    real = env.coord.claim(
        conversation_id=cid,
        request=ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    real_token = real.task["current_assignment"]["lease_token"]
    emit("codex-pca", "task.claimed", f"lease_token={real_token[:8]}...")

    # actor B forges a token and attempts to heartbeat
    forged = "fake-lease-token-deadbeef"
    try:
        env.coord.heartbeat(
            conversation_id=cid,
            request=ChatTaskHeartbeatRequest(
                actor_name="claude-pcb", lease_token=forged, phase="executing",
            ),
        )
        report("01 lease forgery", RESULT_GAP, "forged token accepted (BAD)")
    except Exception as exc:
        report("01 lease forgery", RESULT_PROTECTED,
               f"forged token rejected: {type(exc).__name__}")


def s02_resolution_kind_mismatch(env):
    """PROTECTED. close inquiry as 'completed' (proposal/task vocab)
    -- ChatConversationStateError per PR5 enum."""
    section("02 PROTECTED: resolution-vs-kind enum mismatch")
    thread = env.open_thread("s02")
    inquiry = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    emit("alice", "open", f"inquiry {inquiry.id[:8]}")
    try:
        env.conv.close_conversation(
            conversation_id=inquiry.id, closed_by="alice",
            resolution="completed",  # task vocab, not inquiry
        )
        report("02 res-kind mismatch", RESULT_GAP, "wrong vocab accepted")
    except ChatConversationStateError as exc:
        report("02 res-kind mismatch", RESULT_PROTECTED, f"rejected: {exc}")


def s03_speech_to_closed_conversation(env):
    """PROTECTED. After close, speech is rejected."""
    section("03 PROTECTED: speech to a closed conversation")
    thread = env.open_thread("s03")
    inquiry = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    env.conv.close_conversation(
        conversation_id=inquiry.id, closed_by="alice", resolution="dropped",
    )
    emit("alice", "close", "resolution=dropped")
    try:
        env.conv.submit_speech(
            conversation_id=inquiry.id,
            request=SpeechActSubmitRequest(
                actor_name="bob", kind="claim", content="late comment",
            ),
        )
        report("03 speech on closed", RESULT_GAP, "post-close speech accepted")
    except ChatConversationStateError as exc:
        report("03 speech on closed", RESULT_PROTECTED, f"rejected: {exc}")


def s04_unauthorized_close(env):
    """PROTECTED. PR6 string-match auth: only opener/owner can close."""
    section("04 PROTECTED: unauthorized actor tries to close")
    thread = env.open_thread("s04")
    inquiry = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    emit("alice", "open", f"inquiry by alice")
    try:
        env.conv.close_conversation(
            conversation_id=inquiry.id, closed_by="mallory", resolution="dropped",
        )
        report("04 unauthorized close", RESULT_GAP, "mallory's close accepted")
    except ChatConversationStateError as exc:
        report("04 unauthorized close", RESULT_PROTECTED, f"rejected: {exc}")


def s05_tier3_auto_abandon(env):
    """PROTECTED. 25h-stale conversation gets auto-closed by sweep_idle
    with resolution=abandoned, closed_by=system."""
    section("05 PROTECTED: tier-3 auto-abandon under sustained silence")
    thread = env.open_thread("s05")
    proposal = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="proposal", title="silent", opener_actor="alice",
        ),
    )
    backdate(proposal.id, minutes=25 * 60)
    emit("system", "(simulated)", "backdated 25h")
    flagged = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    if flagged and flagged[0].resolution == "abandoned" and flagged[0].closed_by == "system":
        report("05 tier-3 abandon", RESULT_PROTECTED,
               f"auto-abandoned, system closed (warnings: {flagged[0].idle_warning_count})")
    else:
        report("05 tier-3 abandon", RESULT_GAP,
               f"no abandon despite 25h silence (state: {[f.resolution for f in flagged]})")


def s06_actor_name_spoofing_in_speech(env):
    """GAP. PR6 only governs close/handoff; speech accepts any
    actor_name string. mallory submits as 'alice' -- accepted."""
    section("06 GAP: actor-name spoofing in speech")
    thread = env.open_thread("s06")
    inquiry = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="?", opener_actor="alice",
        ),
    )
    # mallory impersonates alice in a speech submission
    env.conv.submit_speech(
        conversation_id=inquiry.id,
        request=SpeechActSubmitRequest(
            actor_name="alice", kind="claim", content="(actually mallory, faking alice)",
        ),
    )
    emit("mallory", "(impersonating)", "submitted as actor_name='alice'")
    # query the persisted row
    with db_module.session_scope() as session:
        row = session.scalar(
            select(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == inquiry.id)
            .where(ChatMessageModel.event_kind == "chat.speech.claim")
        )
        recorded_actor = row.actor_name if row else None
    if recorded_actor == "alice":
        report("06 actor spoofing", RESULT_GAP,
               "speech recorded as 'alice' though no identity check exists")
    else:
        report("06 actor spoofing", RESULT_PROTECTED, "spoof rejected (unexpected)")


def s07_evidence_injection_by_non_lease_holder(env):
    """GAP. ChatTaskCoordinator.add_evidence does not require a
    lease_token. Any actor can post evidence to any task."""
    section("07 GAP: evidence injection by non-lease-holder")
    thread = env.open_thread("s07")
    task = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="x",
        ),
    )
    env.coord.claim(
        conversation_id=task.id,
        request=ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    emit("codex-pca", "task.claimed", "(holds the lease)")
    # mallory posts evidence without claiming and without a lease
    try:
        env.coord.add_evidence(
            conversation_id=task.id,
            request=ChatTaskEvidenceRequest(
                actor_name="mallory", kind="file_write",
                summary="(planted) deleted /etc/passwd",
            ),
        )
        report("07 evidence injection", RESULT_GAP,
               "non-claimant posted evidence without lease check")
    except Exception as exc:
        report("07 evidence injection", RESULT_PROTECTED,
               f"rejected: {type(exc).__name__}")


def s08_complete_without_evidence(env):
    """GAP. Agent contract says "no working claim without evidence",
    but RemoteTaskService.complete_task accepts a complete with zero
    heartbeats and zero evidence rows."""
    section("08 GAP: task complete without any heartbeat or evidence")
    thread = env.open_thread("s08")
    task = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice",
            objective="ship a critical refactor",
        ),
    )
    response = env.coord.claim(
        conversation_id=task.id,
        request=ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    lease = response.task["current_assignment"]["lease_token"]
    emit("codex-pca", "task.claimed", "no heartbeat, no evidence -- straight to complete")
    completed = env.coord.complete(
        conversation_id=task.id,
        request=ChatTaskCompleteRequest(
            actor_name="codex-pca", lease_token=lease, summary="trust me bro",
        ),
    )
    if completed.task["status"] == "completed":
        # Count evidence rows on this task
        with db_module.session_scope() as session:
            ev_count = session.scalar(
                select(func.count())
                .select_from(ChatMessageModel)
                .where(ChatMessageModel.conversation_id == task.id)
                .where(ChatMessageModel.event_kind == "chat.task.evidence")
            ) or 0
        report("08 complete without evidence", RESULT_GAP,
               f"task accepted complete with {ev_count} evidence rows")
    else:
        report("08 complete without evidence", RESULT_PROTECTED, "rejected")


def s09_evidence_with_arbitrary_kind(env):
    """GAP. ChatTaskEvidenceRequest.kind has no allow-list. An AI can
    invent a kind that won't auto-promote task status (only kinds in
    WORK_EVIDENCE_KINDS do that), but the row still gets persisted as
    'evidence' and counted in metrics."""
    section("09 GAP: evidence with arbitrary/fake kind string")
    thread = env.open_thread("s09")
    task = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="t", opener_actor="alice", objective="x",
        ),
    )
    env.coord.claim(
        conversation_id=task.id,
        request=ChatTaskClaimRequest(actor_name="codex-pca", lease_seconds=120),
    )
    env.coord.add_evidence(
        conversation_id=task.id,
        request=ChatTaskEvidenceRequest(
            actor_name="codex-pca",
            kind="trust_me_bro",  # not a real evidence kind
            summary="believe me, very impressive work",
        ),
    )
    emit("codex-pca", "task.evidence", "kind='trust_me_bro' (not in WORK_EVIDENCE_KINDS)")
    with db_module.session_scope() as session:
        row = session.scalar(
            select(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == task.id)
            .where(ChatMessageModel.event_kind == "chat.task.evidence")
        )
        accepted = row is not None
    if accepted:
        report("09 fake evidence kind", RESULT_GAP,
               "arbitrary kind string accepted; task status not promoted but row persisted")
    else:
        report("09 fake evidence kind", RESULT_PROTECTED, "rejected")


def s10_loop_conversation_never_converges(env):
    """GAP. Idle escalation only fires on silence; a conversation
    where AIs ping-pong forever (each turn under tier-1) keeps
    last_speech_at fresh and never escalates. The
    unaddressed_speech_count gauge bumps but doesn't gate.

    To keep the gauge climbing, alice (the opener) re-addresses to
    'codex-pcb' each round so expected_speaker stays set; claude-pca
    chimes in unaddressed -- that's exactly the off-turn-spam shape
    the gauge is supposed to flag."""
    section("10 GAP: loop conversation that never converges (idle won't fire)")
    thread = env.open_thread("s10")
    inquiry = env.conv.open_conversation(
        discord_thread_id=thread.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="loop", opener_actor="alice",
            addressed_to="codex-pcb",
        ),
    )
    # 6 unaddressed off-turn speeches from claude-pca while bob is
    # supposedly the expected speaker. Gauge should climb to 6.
    for i in range(6):
        env.conv.submit_speech(
            conversation_id=inquiry.id,
            request=SpeechActSubmitRequest(
                actor_name="claude-pca", kind="claim",
                content=f"thinking out loud (round {i + 1})",
            ),
        )
    emit("claude-pca", "speech.claim x6", "off-turn chatter; codex-pcb is expected speaker")
    flagged = env.conv.sweep_idle_conversations(
        discord_thread_id=thread.discord_thread_id,
        idle_threshold_seconds=30 * 60,
    )
    detail = env.conv.get_conversation(conversation_id=inquiry.id).conversation
    # idle won't fire because last_speech_at is fresh; the only signal
    # is unaddressed_speech_count climbing.
    if not flagged and detail.state == "open":
        report(
            "10 loop never converges", RESULT_GAP,
            f"10 unaddressed speech rows, idle not flagged, state still open "
            f"(unaddressed_count={detail.unaddressed_speech_count})",
        )
    else:
        report("10 loop never converges", RESULT_PROTECTED, f"flagged: {flagged}")


def _all_conversations() -> list[ChatConversationModel]:
    """Snapshot helper; only safe to use the .id attribute outside the
    session."""
    with db_module.session_scope() as session:
        return list(session.scalars(select(ChatConversationModel)))


SCENARIOS: list[tuple[str, Callable[[Env], None]]] = [
    ("01 lease forgery", s01_lease_forgery_on_heartbeat),
    ("02 resolution-kind mismatch", s02_resolution_kind_mismatch),
    ("03 speech to closed", s03_speech_to_closed_conversation),
    ("04 unauthorized close", s04_unauthorized_close),
    ("05 tier-3 auto-abandon", s05_tier3_auto_abandon),
    ("06 actor spoofing", s06_actor_name_spoofing_in_speech),
    ("07 evidence injection", s07_evidence_injection_by_non_lease_holder),
    ("08 complete without evidence", s08_complete_without_evidence),
    ("09 fake evidence kind", s09_evidence_with_arbitrary_kind),
    ("10 loop never converges", s10_loop_conversation_never_converges),
]


def main() -> int:
    db_module.init_db()
    env = Env()

    section("Boot")
    print(f"  tmp db = {os.environ['BRIDGE_DATABASE_URL']}")

    crashes: list[tuple[str, str]] = []
    for name, fn in SCENARIOS:
        try:
            fn(env)
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc().splitlines()[-3:]
            crashes.append((name, f"{type(exc).__name__}: {exc}"))
            emit("scenario", "CRASH", f"{name} -- {exc}")

    section("Summary")
    protected = [r for r in _results if r["kind"] == RESULT_PROTECTED]
    gaps = [r for r in _results if r["kind"] == RESULT_GAP]

    print(f"  total scenarios:  {len(SCENARIOS)}")
    print(f"  PROTECTED (system rejected/handled the abuse):  {len(protected)}")
    print(f"  GAP       (abuse accepted -- protocol weakness):  {len(gaps)}")
    print(f"  CRASH     (scenario errored unexpectedly):        {len(crashes)}")

    if protected:
        print()
        print("  PROTECTED:")
        for r in protected:
            print(f"    [OK] {r['label']:<30} {r['detail']}")
    if gaps:
        print()
        print("  GAPS (action items):")
        for r in gaps:
            print(f"    [!!] {r['label']:<30} {r['detail']}")
    if crashes:
        print()
        print("  CRASHES:")
        for name, msg in crashes:
            print(f"    [XX] {name:<30} {msg}")

    print()
    print("  hardening priorities (descending):")
    print("    1. evidence/heartbeat lease check (gap #07) -- evidence path")
    print("       is currently un-gated; any actor can plant evidence on any")
    print("       task. Fix: require lease_token on add_evidence the way")
    print("       heartbeat/complete already do.")
    print("    2. actor identity binding (gap #06) -- the bridge token")
    print("       authenticates the *caller* but not the *speaker*. Either")
    print("       map bridge tokens to permitted actor_names or require a")
    print("       per-actor signing key.")
    print("    3. complete-without-evidence policy (gap #08) -- the agent")
    print("       contract says \"no progress claim without evidence\" but the")
    print("       system never checks. Fix: reject complete_task when zero")
    print("       evidence rows exist (or warn + tag the row).")
    print("    4. evidence kind allow-list (gap #09) -- enum the kind set")
    print("       so 'trust_me_bro' fails at the schema layer, not silently")
    print("       persists.")
    print("    5. loop / convergence pressure (gap #10) -- idle escalation")
    print("       only catches silence; need a separate \"too much chatter")
    print("       no decision\" tier (e.g. >=N unaddressed speech without a")
    print("       close emits a different escalation kind).")

    return 0 if not crashes and not gaps else 1


if __name__ == "__main__":
    sys.exit(main())
