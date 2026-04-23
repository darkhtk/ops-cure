from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from contextlib import suppress
import asyncio

from fastapi import FastAPI

from .api import actors, behaviors, events, health, presence, remote_tasks, sessions, spaces, verification, workers
from .behaviors.catalog import BehaviorCatalogService
from .behaviors.chat import api as chat_api
from .behaviors.chat.service import ChatBehaviorService
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
from .kernel.registry import WorkerRegistry
from .kernel.spaces import SpaceService
from .kernel.subscriptions import InProcessSubscriptionBroker
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
    space_service: SpaceService
    thread_manager: ThreadManager
    announcement_service: AnnouncementService
    chat_service: ChatBehaviorService
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
    session_service: SessionService
    discord_gateway: DiscordGateway
    recovery_loop_task: asyncio.Task[None] | None = None


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
    remote_task_service = RemoteTaskService(presence_service=presence_service)
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
        thread_manager=thread_manager,
        announcement_service=announcement_service,
        chat_service=chat_service,
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
        session_service=session_service,
        discord_gateway=discord_gateway,
        space_service=space_service,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    services = build_services(settings)
    app.state.services = services
    services.recovery_loop_task = asyncio.create_task(
        services.recovery_service.run_forever(
            interval_seconds=settings.recovery_loop_interval_seconds,
        ),
        name="opscure-recovery-loop",
    )
    await services.discord_gateway.start()
    try:
        yield
    finally:
        services.recovery_service.stop()
        if services.recovery_loop_task is not None:
            with suppress(asyncio.CancelledError):
                await services.recovery_loop_task
        await services.discord_gateway.stop()


app = FastAPI(title="Opscure Bridge", lifespan=lifespan)
app.include_router(health.router)
app.include_router(actors.router)
app.include_router(behaviors.router)
app.include_router(events.router)
app.include_router(presence.router)
app.include_router(remote_tasks.router)
app.include_router(chat_api.router)
app.include_router(sessions.router)
app.include_router(spaces.router)
app.include_router(verification.router)
app.include_router(workers.router)
