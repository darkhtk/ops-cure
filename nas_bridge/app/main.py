from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from contextlib import suppress
import asyncio
from typing import Any

from fastapi import FastAPI

from .api import actors, behaviors, events, health, kernel_approvals, kernel_scratch, kernel_tasks, presence, remote_tasks, sessions, spaces, v2_diagnostics, v2_inbox, v2_operations, verification, workers
from .behaviors.catalog import BehaviorCatalogService
from .behaviors.chat import api as chat_api
from .behaviors.remote_codex import api as remote_codex_api
from .behaviors.remote_codex.service import RemoteCodexBehaviorService
from .behaviors.remote_claude import api as remote_claude_api
from .behaviors.remote_claude.service import RemoteClaudeBehaviorService
from .behaviors.remote_claude.state_service import RemoteClaudeStateService
from .behaviors.chat.conversation_service import ChatConversationService
from .behaviors.chat.service import ChatBehaviorService
from .behaviors.chat.task_coordinator import ChatTaskCoordinator
from .behaviors.orchestration.policy import PolicyService
from .behaviors.orchestration.recovery import RecoveryService
from .behaviors.orchestration.service import SessionService
from .behaviors.orchestration.verification import VerificationService
from .behaviors.orchestration.workflows.pause import PauseWorkflow
from .behaviors.orchestration.workflows.policy import PolicyWorkflow
from .behaviors.orchestration.workflows.start import StartWorkflow
from .behaviors.ops.service import OpsBehaviorService
from .behaviors.registry import (
    BehaviorContext,
    BehaviorDescriptor,
    default_behavior_descriptors,
    resolve_discord_bindings,
    resolve_kernel_bindings,
)
from .capabilities.execution.router import RoutedExecutionProvider
from .capabilities.execution.windows_launcher import WindowsLauncherExecutionProvider
from .capabilities.power.noop import NoopPowerProvider
from .capabilities.power.router import RoutedPowerProvider
from .capabilities.power.wol import WakeOnLanPowerProvider
from .config import Settings, get_settings
from .kernel.actors import ActorService
from .kernel.bindings import KernelBehaviorBinding
from .kernel.drift import DriftMonitor
from .kernel.event_log import TranscriptService
from .kernel.events import EventService
from .kernel.presence import PresenceService
from .kernel.approvals import KernelApprovalService
from .kernel.registry import WorkerRegistry
from .kernel.scratch import KernelScratchService
from .kernel.spaces import SpaceService
from .kernel.subscriptions import InProcessSubscriptionBroker
from .kernel.tasks import KernelTaskService
from .presenters.discord.status_cards import AnnouncementService
from .services.remote_task_service import RemoteTaskService
from .transports.discord.gateway import DiscordGateway
from .transports.discord.bindings import DiscordBehaviorBinding
from .transports.discord.threads import ThreadManager
from .db import init_db


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@dataclass(slots=True)
class ServiceContainer:
    settings: Settings
    registry: WorkerRegistry
    transcript_service: TranscriptService
    subscription_broker: InProcessSubscriptionBroker
    actor_service: ActorService
    event_service: EventService
    kernel_approval_service: KernelApprovalService
    kernel_scratch_service: KernelScratchService
    kernel_task_service: KernelTaskService
    space_service: SpaceService
    thread_manager: ThreadManager
    announcement_service: AnnouncementService
    chat_service: ChatBehaviorService
    chat_conversation_service: ChatConversationService
    chat_task_coordinator: ChatTaskCoordinator
    ops_service: OpsBehaviorService
    behavior_descriptors: tuple[BehaviorDescriptor, ...]
    kernel_behaviors: list[KernelBehaviorBinding]
    discord_behaviors: list[DiscordBehaviorBinding]
    behavior_catalog_service: BehaviorCatalogService
    presence_service: PresenceService
    policy_service: PolicyService
    recovery_service: RecoveryService
    verification_service: VerificationService
    remote_task_service: RemoteTaskService
    remote_codex_service: RemoteCodexBehaviorService
    remote_claude_service: RemoteClaudeBehaviorService
    session_service: SessionService
    discord_gateway: DiscordGateway
    recovery_loop_task: asyncio.Task[None] | None = None
    # H5: optional periodic digest poster + its task handle
    digest_scheduler: Any | None = None
    digest_loop_task: asyncio.Task[None] | None = None


def build_services(settings: Settings) -> ServiceContainer:
    configure_logging(settings.log_level)
    init_db()
    registry = WorkerRegistry(settings.worker_stale_after_seconds)
    drift_monitor = DriftMonitor()
    subscription_broker = InProcessSubscriptionBroker()
    transcript_service = TranscriptService(subscription_broker=subscription_broker)
    thread_manager = ThreadManager(settings)
    announcement_service = AnnouncementService(thread_manager=thread_manager)
    chat_service = ChatBehaviorService(thread_manager=thread_manager, subscription_broker=subscription_broker)
    ops_service = OpsBehaviorService(thread_manager=thread_manager, subscription_broker=subscription_broker)
    policy_service = PolicyService()
    verification_service = VerificationService(
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
    )
    presence_service = PresenceService()
    kernel_approval_service = KernelApprovalService()
    kernel_scratch_service = KernelScratchService()
    kernel_task_service = KernelTaskService()
    remote_task_service = RemoteTaskService(
        presence_service=presence_service,
        kernel_approval_service=kernel_approval_service,
    )
    # H1: per-capability authorization across every entry point.
    # The 3-arg authorizer is consulted with the action's capability
    # by ChatConversationService.check_capability and the task
    # coordinator's claim/complete/fail/approve_destructive paths.
    # G1's earlier per-call-site capability=SPEECH_SUBMIT closure is
    # superseded.
    from .kernel.v2 import CapabilityService, make_per_capability_authorizer

    capability_service = CapabilityService()
    chat_conversation_service = ChatConversationService(
        subscription_broker=subscription_broker,
        remote_task_service=remote_task_service,
        capability_authorizer=make_per_capability_authorizer(capability_service),
    )
    chat_conversation_service.backfill_general_conversations()
    chat_task_coordinator = ChatTaskCoordinator(
        conversation_service=chat_conversation_service,
        remote_task_service=remote_task_service,
        subscription_broker=subscription_broker,
    )
    remote_codex_service = RemoteCodexBehaviorService(
        remote_task_service=remote_task_service,
        kernel_subscription_broker=subscription_broker,
        kernel_task_service=kernel_task_service,
    )
    remote_claude_state_service = RemoteClaudeStateService(
        kernel_subscription_broker=subscription_broker,
    )
    remote_claude_service = RemoteClaudeBehaviorService(
        state_service=remote_claude_state_service,
    )
    power_provider = RoutedPowerProvider([NoopPowerProvider(), WakeOnLanPowerProvider()])
    execution_provider = RoutedExecutionProvider([WindowsLauncherExecutionProvider(registry)])
    recovery_service = RecoveryService(
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
        power_provider=power_provider,
        execution_provider=execution_provider,
        worker_stale_after_seconds=settings.worker_stale_after_seconds,
        stalled_start_timeout_seconds=settings.stalled_start_timeout_seconds,
    )
    session_service = SessionService(
        registry=registry,
        thread_manager=thread_manager,
        transcript_service=transcript_service,
        drift_monitor=drift_monitor,
        presence_service=presence_service,
    )
    announcement_service.bind_summary_provider(session_service.get_session_summary)
    start_workflow = StartWorkflow(
        session_service=session_service,
        policy_service=policy_service,
        recovery_service=recovery_service,
        announcement_service=announcement_service,
    )
    pause_workflow = PauseWorkflow(
        recovery_service=recovery_service,
        transcript_service=transcript_service,
        announcement_service=announcement_service,
    )
    policy_workflow = PolicyWorkflow(
        session_service=session_service,
        policy_service=policy_service,
        announcement_service=announcement_service,
    )
    session_service.bind_orchestration(
        policy_service=policy_service,
        recovery_service=recovery_service,
        start_workflow=start_workflow,
        pause_workflow=pause_workflow,
        policy_workflow=policy_workflow,
        execution_provider=execution_provider,
        announcement_service=announcement_service,
    )
    behavior_descriptors = default_behavior_descriptors()
    behavior_context = BehaviorContext(
        registry=registry,
        thread_manager=thread_manager,
        chat_service=chat_service,
        ops_service=ops_service,
        policy_service=policy_service,
        recovery_service=recovery_service,
        session_service=session_service,
        verification_service=verification_service,
    )
    kernel_behaviors = resolve_kernel_bindings(
        context=behavior_context,
        descriptors=behavior_descriptors,
    )
    actor_service = ActorService(
        providers=[
            binding.actor_provider
            for binding in kernel_behaviors
            if binding.actor_provider is not None
        ],
    )
    event_service = EventService(
        providers=[
            binding.event_provider
            for binding in kernel_behaviors
            if binding.event_provider is not None
        ],
    )
    space_service = SpaceService(
        providers=[
            binding.space_provider
            for binding in kernel_behaviors
            if binding.space_provider is not None
        ],
    )
    discord_behaviors = resolve_discord_bindings(
        context=behavior_context,
        descriptors=behavior_descriptors,
    )
    behavior_catalog_service = BehaviorCatalogService(
        descriptors=behavior_descriptors,
        kernel_bindings=kernel_behaviors,
        discord_bindings=discord_behaviors,
    )
    discord_gateway = DiscordGateway(
        settings=settings,
        behavior_bindings=discord_behaviors,
        thread_manager=thread_manager,
    )
    return ServiceContainer(
        settings=settings,
        registry=registry,
        transcript_service=transcript_service,
        subscription_broker=subscription_broker,
        actor_service=actor_service,
        event_service=event_service,
        kernel_approval_service=kernel_approval_service,
        kernel_scratch_service=kernel_scratch_service,
        kernel_task_service=kernel_task_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
        chat_service=chat_service,
        chat_conversation_service=chat_conversation_service,
        chat_task_coordinator=chat_task_coordinator,
        ops_service=ops_service,
        behavior_descriptors=behavior_descriptors,
        kernel_behaviors=kernel_behaviors,
        discord_behaviors=discord_behaviors,
        behavior_catalog_service=behavior_catalog_service,
        presence_service=presence_service,
        policy_service=policy_service,
        recovery_service=recovery_service,
        verification_service=verification_service,
        remote_task_service=remote_task_service,
        remote_codex_service=remote_codex_service,
        remote_claude_service=remote_claude_service,
        session_service=session_service,
        discord_gateway=discord_gateway,
        space_service=space_service,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.chat_v1_deprecation_warning:
        logging.getLogger("opscure.deprecation").warning(
            "[v1 chat deprecation] Protocol v2 (operations_v2 + /v2 routes) "
            "is now the system of record after F7. Legacy /chat endpoints "
            "remain dual-written through F8 but will be removed once /v2 "
            "covers all client surfaces. Migrate to GET /v2/inbox, "
            "GET /v2/operations/{id}, and POST /v2/operations/{id}/seen. "
            "Set BRIDGE_CHAT_V1_DEPRECATION_WARNING=false to silence."
        )
    services = build_services(settings)
    app.state.services = services
    services.recovery_loop_task = asyncio.create_task(
        services.recovery_service.run_forever(
            interval_seconds=settings.recovery_loop_interval_seconds,
        ),
        name="opscure-recovery-loop",
    )
    await services.discord_gateway.start()
    # NOTE: in-process agent runners were removed. Agents are external
    # clients of the kernel: they subscribe to /v2/inbox/stream by actor
    # handle and post speech.claim back via /v2/operations/{id}/events.
    # See pc_launcher/connectors/claude_executor/agent_loop.py.
    # H5: digest cron loop. opt-in via BRIDGE_DIGEST_INTERVAL_SECONDS;
    # 0/unset disables. Default off to keep test env quiet; production
    # sets 86400 (daily).
    import os as _os
    digest_interval_raw = _os.environ.get("BRIDGE_DIGEST_INTERVAL_SECONDS", "0")
    try:
        digest_interval = int(digest_interval_raw)
    except ValueError:
        digest_interval = 0
    if digest_interval > 0:
        from .behaviors.digest import DigestSchedulerLoop
        digest_scheduler = DigestSchedulerLoop(
            chat_service=services.chat_conversation_service,
            interval_seconds=digest_interval,
        )
        services.digest_scheduler = digest_scheduler
        services.digest_loop_task = asyncio.create_task(
            digest_scheduler.run_forever(),
            name="opscure-digest-loop",
        )
    try:
        yield
    finally:
        services.recovery_service.stop()
        if services.recovery_loop_task is not None:
            with suppress(asyncio.CancelledError):
                await services.recovery_loop_task
        if getattr(services, "digest_scheduler", None) is not None:
            services.digest_scheduler.stop()
        if getattr(services, "digest_loop_task", None) is not None:
            services.digest_loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await services.digest_loop_task
        await services.discord_gateway.stop()


app = FastAPI(title="Opscure Bridge", lifespan=lifespan)
app.include_router(health.router)
app.include_router(actors.router)
app.include_router(behaviors.router)
app.include_router(events.router)
app.include_router(kernel_approvals.router)
app.include_router(kernel_scratch.router)
app.include_router(kernel_tasks.router)
app.include_router(presence.router)
app.include_router(remote_tasks.router)
app.include_router(remote_codex_api.router)
app.include_router(remote_claude_api.router)
app.include_router(chat_api.router)
app.include_router(sessions.router)
app.include_router(spaces.router)
app.include_router(verification.router)
app.include_router(v2_inbox.router)
app.include_router(v2_operations.router)
app.include_router(v2_diagnostics.router)
app.include_router(workers.router)
