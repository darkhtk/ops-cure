"""Real reactive AI-collab demo.

Unlike the other scenario scripts in this directory which are
scripted choreography (each speech act is hardcoded by the author),
THIS demo runs autonomous rule-based agents on top of the protocol:

- Each agent subscribes to the kernel events broker.
- Each agent decides what to do based on what it observes.
- The transcript that emerges is NOT pre-written -- it's the result
  of agents reacting to each other's events.

Limitations honest up front:
- Agents are rule-based, not LLM-driven. The decision logic is
  deterministic if/elif on event kind + content. No real natural-
  language generation. But the COLLABORATION SHAPE is real:
  observe -> decide -> act -> trigger -> observe.
- One scenario only -- the point is to demonstrate emergent
  multi-agent behavior, not to enumerate all combinations.

Run:  python scripts/reactive_agents_demo.py

The agents:
- HumanScripter (alice)        -- triggers the scenario; opens a
                                  task and a question; closes when
                                  things are done. Stand-in for a
                                  human typing in Discord.
- EagerImplementer (claude-pca) -- when sees a kind=task, claims it
                                  and runs through the lifecycle
                                  with realistic delays + evidence.
- SkepticalReviewer (codex-pcb) -- watches all proposals. Objects
                                  if title contains 'delete' or
                                  'rollback'; agrees otherwise.
- HelpfulAnswerer (gemini-pcc)  -- when sees an inquiry addressed_to
                                  itself, answers after a short
                                  think.

Loop: alice triggers, agents observe + react, more events fire,
more agents react. Loop runs until alice signals "scenario done".
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
NAS_BRIDGE_ROOT = REPO_ROOT / "nas_bridge"
if str(NAS_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAS_BRIDGE_ROOT))

_TMP_DIR = tempfile.mkdtemp(prefix="opscure_reactive_")
os.environ["BRIDGE_SHARED_AUTH_TOKEN"] = "demo-token"
os.environ["BRIDGE_DISABLE_DISCORD"] = "true"
os.environ["BRIDGE_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR, 'reactive.db').as_posix()}"

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
from app.behaviors.chat.metrics import ChatRoomMetrics  # noqa: E402
from app.behaviors.chat.models import ChatConversationModel  # noqa: E402
from app.behaviors.chat.service import ChatBehaviorService  # noqa: E402
from app.behaviors.chat.task_coordinator import ChatTaskCoordinator  # noqa: E402
from app.kernel.approvals import KernelApprovalService  # noqa: E402
from app.kernel.events import EventEnvelope  # noqa: E402
from app.kernel.presence import PresenceService  # noqa: E402
from app.kernel.subscriptions import InProcessSubscriptionBroker  # noqa: E402
from app.services.remote_task_service import RemoteTaskService  # noqa: E402


# ---------------------------------------------------------------------------
# Stub thread manager


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


# ---------------------------------------------------------------------------
# Live transcript printer


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def emit(actor: str, action: str, detail: str = "") -> None:
    print(f"  {now_str()}  {actor:<16}  {action:<30}  {detail}")


# ---------------------------------------------------------------------------
# Agent base


class Agent:
    """Base class for autonomous reactive agents.

    Each agent runs as an asyncio task that pulls events from its
    broker subscription and dispatches to ``react``. ``react`` is the
    only method subclasses override; it returns nothing (side effects
    via the env's services, identified by ``self.name``)."""

    name: str = "agent"

    def __init__(self, env: "ReactiveEnv") -> None:
        self.env = env
        self.subscription = env.broker.subscribe(
            space_id=env.thread_uuid,
            subscriber_id=f"agent:{self.name}",
        )
        self._task: asyncio.Task | None = None
        # Per-agent local state (e.g. "I am holding lease X for task Y").
        self.held_leases: dict[str, str] = {}  # task_conv_id -> lease_token

    async def run(self, stop_event: asyncio.Event) -> None:
        emit(self.name, "(joined)", f"subscribed to thread space")
        while not stop_event.is_set():
            envelope = await self.subscription.next_event(timeout_seconds=0.05)
            if envelope is None:
                continue
            if envelope.event.actor_name == self.name:
                # Don't react to my own events
                continue
            try:
                await self.react(envelope)
            except Exception as exc:  # noqa: BLE001
                emit(self.name, "(error)", f"{type(exc).__name__}: {exc}")
        self.subscription.close()

    async def react(self, envelope: EventEnvelope) -> None:
        raise NotImplementedError


def parse_payload(envelope: EventEnvelope) -> dict[str, Any]:
    """Conversation-lifecycle / task events store JSON in content;
    speech events store plain text. Try to parse as JSON, fall back
    to {} for plain-text speech."""
    try:
        return json.loads(envelope.event.content)
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Concrete agents


class EagerImplementer(Agent):
    """When a kind=task conversation opens, claim it. Then run
    through heartbeat + evidence + complete on a realistic timeline."""

    name = "claude-pca"

    async def react(self, envelope: EventEnvelope) -> None:
        if envelope.event.kind == "chat.conversation.opened":
            payload = parse_payload(envelope)
            if payload.get("kind") == "task":
                conv_id = payload["id"]
                # Race-aware: try to claim, accept rejection silently
                # (another agent may have grabbed it).
                try:
                    response = self.env.coord.claim(
                        conversation_id=conv_id,
                        request=ChatTaskClaimRequest(
                            actor_name=self.name, lease_seconds=60,
                        ),
                    )
                    lease = response.task["current_assignment"]["lease_token"]
                    self.held_leases[conv_id] = lease
                except Exception as exc:  # noqa: BLE001
                    emit(self.name, "claim.failed", f"{type(exc).__name__}")
                    return
                # Schedule work as a background task so this react()
                # returns promptly and we continue listening.
                asyncio.create_task(self._do_work(conv_id, lease))

    async def _do_work(self, conv_id: str, lease: str) -> None:
        await asyncio.sleep(0.1)
        # heartbeat
        self.env.coord.heartbeat(
            conversation_id=conv_id,
            request=ChatTaskHeartbeatRequest(
                actor_name=self.name, lease_token=lease,
                phase="executing", summary="reading code",
                files_read_count=3,
            ),
        )
        await asyncio.sleep(0.15)
        # evidence
        self.env.coord.add_evidence(
            conversation_id=conv_id,
            request=ChatTaskEvidenceRequest(
                actor_name=self.name, lease_token=lease,
                kind="file_write",
                summary="patched the target module",
            ),
        )
        await asyncio.sleep(0.1)
        # complete
        self.env.coord.complete(
            conversation_id=conv_id,
            request=ChatTaskCompleteRequest(
                actor_name=self.name, lease_token=lease,
                summary="done; tests pass",
            ),
        )


class SkepticalReviewer(Agent):
    """Watches proposals. If the title contains a destructive
    keyword (delete/rollback/drop), object after a short pause.
    Otherwise agree. Demonstrates rule-based content reaction."""

    name = "codex-pcb"
    DESTRUCTIVE = ("delete", "rollback", "drop")

    async def react(self, envelope: EventEnvelope) -> None:
        if envelope.event.kind == "chat.conversation.opened":
            payload = parse_payload(envelope)
            if payload.get("kind") == "proposal":
                conv_id = payload["id"]
                title = (payload.get("title") or "").lower()
                # think for a beat (LLM-stand-in)
                await asyncio.sleep(0.2)
                if any(word in title for word in self.DESTRUCTIVE):
                    self.env.conv.submit_speech(
                        conversation_id=conv_id,
                        request=SpeechActSubmitRequest(
                            actor_name=self.name, kind="object",
                            content=f"this is destructive ({title!r}); "
                                    f"needs evidence + approval before I'd "
                                    f"sign off",
                        ),
                    )
                else:
                    self.env.conv.submit_speech(
                        conversation_id=conv_id,
                        request=SpeechActSubmitRequest(
                            actor_name=self.name, kind="agree",
                            content="reviewed; LGTM",
                        ),
                    )


class HelpfulAnswerer(Agent):
    """When sees an inquiry addressed_to=self, answer after a short
    think. Has a small built-in knowledge map of canned answers."""

    name = "gemini-pcc"
    KNOWLEDGE = {
        "lease": "leases are PresenceService-owned; check the lease_token "
                 "against current assignment before any mutating call.",
        "auth": "PR6 added string-match closer auth; PR13 added an "
                "actor_authorizer callback for production identity binding.",
        "metrics": "ChatRoomMetrics is in-memory; capture_metric_snapshot "
                   "persists to chat_metric_snapshots.",
    }

    async def react(self, envelope: EventEnvelope) -> None:
        if envelope.event.kind == "chat.conversation.opened":
            payload = parse_payload(envelope)
            if (payload.get("kind") == "inquiry"
                    and payload.get("expected_speaker") == self.name):
                conv_id = payload["id"]
                title = (payload.get("title") or "").lower()
                await asyncio.sleep(0.15)
                hit = next(
                    (snip for keyword, snip in self.KNOWLEDGE.items()
                     if keyword in title), None,
                )
                if hit:
                    self.env.conv.submit_speech(
                        conversation_id=conv_id,
                        request=SpeechActSubmitRequest(
                            actor_name=self.name, kind="answer",
                            content=hit, addressed_to=payload.get("opener_actor"),
                        ),
                    )
                else:
                    self.env.conv.submit_speech(
                        conversation_id=conv_id,
                        request=SpeechActSubmitRequest(
                            actor_name=self.name, kind="defer",
                            content=f"don't have a canned answer for {title!r}; "
                                    f"escalating to claude-pca",
                            addressed_to="claude-pca",
                        ),
                    )


# ---------------------------------------------------------------------------
# Environment


class ReactiveEnv:
    def __init__(self) -> None:
        self.thread_manager = StubThreadManager()
        self.broker = InProcessSubscriptionBroker(presence_ttl_seconds=60)
        self.chat = ChatBehaviorService(
            thread_manager=self.thread_manager, subscription_broker=self.broker,
        )
        self.presence = PresenceService()
        self.approvals = KernelApprovalService()
        self.remote_task = RemoteTaskService(
            presence_service=self.presence, kernel_approval_service=self.approvals,
        )
        self.metrics = ChatRoomMetrics()
        self.conv = ChatConversationService(
            remote_task_service=self.remote_task,
            subscription_broker=self.broker,
            metrics=self.metrics,
        )
        self.coord = ChatTaskCoordinator(
            conversation_service=self.conv, remote_task_service=self.remote_task,
            subscription_broker=self.broker,
        )
        self.thread_uuid: str = ""
        self.discord_thread_id: str = ""

    async def open_room(self, title: str = "reactive-room") -> None:
        thread = await self.chat.create_chat_thread(
            guild_id="g", parent_channel_id="p", title=title,
            topic=None, created_by="alice",
        )
        self.thread_uuid = thread.id
        self.discord_thread_id = thread.discord_thread_id


# ---------------------------------------------------------------------------
# Human scripter (drives the scenario)


async def human_scripter(env: ReactiveEnv, stop_event: asyncio.Event) -> None:
    """alice's actions -- the only "scripted" part. She triggers
    events and waits for the agents to react. Each step waits for
    something OBSERVABLE (e.g. task completed) before the next
    trigger -- proving the agents really did the work."""
    name = "alice"
    emit(name, "(human)", "starting scenario")

    # Step 1: ask gemini a question. helpful answerer should reply.
    await asyncio.sleep(0.05)
    inq = env.conv.open_conversation(
        discord_thread_id=env.discord_thread_id,
        request=ConversationOpenRequest(
            kind="inquiry", title="How does the lease lifecycle work?",
            opener_actor=name, addressed_to="gemini-pcc",
        ),
    )
    emit(name, "open.inquiry", f"@gemini-pcc")

    # Wait for gemini to actually answer (not us asserting, observing)
    answered_at = await wait_for_event(
        env.broker, env.thread_uuid,
        match=lambda e: (
            e.event.kind == "chat.speech.answer"
            and e.event.actor_name == "gemini-pcc"
        ),
        timeout=2.0,
    )
    if answered_at:
        env.conv.close_conversation(
            conversation_id=inq.id, closed_by=name, resolution="answered",
        )
        emit(name, "close.inquiry", "after gemini answered")

    # Step 2: open a task. eager implementer should claim + complete.
    await asyncio.sleep(0.1)
    task = env.conv.open_conversation(
        discord_thread_id=env.discord_thread_id,
        request=ConversationOpenRequest(
            kind="task", title="Patch the auth middleware",
            opener_actor=name, objective="apply patch + tests",
        ),
    )
    emit(name, "open.task", "Patch the auth middleware")

    # Wait for the task to complete -- alice doesn't drive any of
    # claim/heartbeat/evidence/complete. The agent does it.
    completed = await wait_for_event(
        env.broker, env.thread_uuid,
        match=lambda e: (
            e.event.kind == "chat.task.completed"
            and parse_payload(e).get("taskId") == task.bound_task_id
        ),
        timeout=3.0,
    )
    if completed:
        emit(name, "(observed)", "task completed by claude-pca; conversation auto-closed")

    # Step 3: open a destructive proposal. skeptical reviewer should object.
    await asyncio.sleep(0.1)
    proposal = env.conv.open_conversation(
        discord_thread_id=env.discord_thread_id,
        request=ConversationOpenRequest(
            kind="proposal", title="Delete user_events older than 90d",
            opener_actor=name,
        ),
    )
    emit(name, "open.proposal", "Delete user_events older than 90d")
    objected = await wait_for_event(
        env.broker, env.thread_uuid,
        match=lambda e: (
            e.event.kind == "chat.speech.object"
            and e.event.actor_name == "codex-pcb"
        ),
        timeout=2.0,
    )
    if objected:
        env.conv.close_conversation(
            conversation_id=proposal.id, closed_by=name,
            resolution="withdrawn",
            summary="codex flagged it as destructive; rethinking",
        )
        emit(name, "close.proposal", "withdrawn after codex objected")

    # Step 4: a non-destructive proposal. skeptical reviewer should agree.
    await asyncio.sleep(0.1)
    p2 = env.conv.open_conversation(
        discord_thread_id=env.discord_thread_id,
        request=ConversationOpenRequest(
            kind="proposal", title="Bump pytest to 8.5",
            opener_actor=name,
        ),
    )
    emit(name, "open.proposal", "Bump pytest to 8.5")
    agreed = await wait_for_event(
        env.broker, env.thread_uuid,
        match=lambda e: (
            e.event.kind == "chat.speech.agree"
            and e.event.actor_name == "codex-pcb"
            and parse_payload(e).get("conversationId") != proposal.id
        ),
        timeout=2.0,
    )
    if agreed:
        env.conv.close_conversation(
            conversation_id=p2.id, closed_by=name, resolution="accepted",
        )
        emit(name, "close.proposal", "accepted after codex agreed")

    await asyncio.sleep(0.2)  # let lingering events flush
    emit(name, "(human)", "scenario complete; signaling stop")
    stop_event.set()


async def wait_for_event(
    broker: InProcessSubscriptionBroker,
    space_id: str,
    *,
    match: Callable[[EventEnvelope], bool],
    timeout: float,
) -> EventEnvelope | None:
    """Subscribe transiently to wait for a specific event."""
    sub = broker.subscribe(space_id=space_id, subscriber_id="scripter-wait")
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = max(0.0, deadline - asyncio.get_event_loop().time())
            ev = await sub.next_event(timeout_seconds=min(remaining, 0.1))
            if ev is not None and match(ev):
                return ev
        return None
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# Main


async def amain() -> int:
    db_module.init_db()
    env = ReactiveEnv()
    await env.open_room(title="reactive-collab")

    print()
    print("=" * 78)
    print("  Reactive Agents Demo")
    print("  -- alice triggers events; agents react autonomously")
    print("=" * 78)

    stop = asyncio.Event()
    agents = [
        EagerImplementer(env),
        SkepticalReviewer(env),
        HelpfulAnswerer(env),
    ]
    agent_tasks = [asyncio.create_task(a.run(stop)) for a in agents]
    scripter_task = asyncio.create_task(human_scripter(env, stop))

    await scripter_task
    # give agents a moment to flush
    await asyncio.sleep(0.3)
    for t in agent_tasks:
        t.cancel()
    for t in agent_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass

    # Verify the EMERGENT shape of the room
    print()
    print("=" * 78)
    print("  Emergent room state (driven by agents, not script)")
    print("=" * 78)
    from sqlalchemy import select, func
    from app.behaviors.chat.models import ChatMessageModel
    with db_module.session_scope() as s:
        room_dicts = [
            {"kind": r.kind, "state": r.state, "resolution": r.resolution, "title": r.title}
            for r in s.scalars(
                select(ChatConversationModel)
                .where(ChatConversationModel.thread_id == env.thread_uuid)
                .order_by(ChatConversationModel.created_at.asc())
            )
        ]
        rows = s.execute(
            select(ChatMessageModel.actor_name, func.count())
            .where(ChatMessageModel.thread_id == env.thread_uuid)
            .where(ChatMessageModel.event_kind.like("chat.speech.%"))
            .group_by(ChatMessageModel.actor_name)
        ).all()
        speech_count_by_actor = {actor: int(count) for actor, count in rows}
        task_completes = s.scalar(
            select(func.count())
            .select_from(ChatMessageModel)
            .where(ChatMessageModel.thread_id == env.thread_uuid)
            .where(ChatMessageModel.event_kind == "chat.task.completed")
        ) or 0

    for room in room_dicts:
        print(f"  [{room['kind']:<8}] {room['state']:<6} {room['resolution'] or '-':<12} \"{room['title']}\"")

    print()
    print(f"  speech by actor (driven by agent rules, NOT pre-scripted):")
    for actor, count in sorted(speech_count_by_actor.items()):
        print(f"    {actor:<14} {count}")
    print(f"  task completions (claude-pca acted autonomously): {task_completes}")
    print()

    # Smoke asserts: prove the emergence happened
    failures: list[str] = []
    if speech_count_by_actor.get("gemini-pcc", 0) < 1:
        failures.append("gemini-pcc did not answer the inquiry")
    if speech_count_by_actor.get("codex-pcb", 0) < 2:
        failures.append("codex-pcb did not respond to both proposals")
    if task_completes < 1:
        failures.append("claude-pca did not complete the task")
    closed_resolutions = [r["resolution"] for r in room_dicts if r["resolution"]]
    if "completed" not in closed_resolutions:
        failures.append("no task closed as completed")
    if "withdrawn" not in closed_resolutions:
        failures.append("destructive proposal not withdrawn")
    if "accepted" not in closed_resolutions:
        failures.append("non-destructive proposal not accepted")

    if failures:
        print("  EMERGENCE CHECK: FAIL")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  EMERGENCE CHECK: ok")
    print("  - alice opened 4 conversations; agents drove every other action")
    print("  - claude-pca claimed + heartbeat'd + evidenced + completed without script")
    print("  - codex-pcb agreed on safe proposals, objected to destructive ones")
    print("  - gemini-pcc answered the addressed inquiry from its KB")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
