from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from contextlib import suppress
import asyncio

from fastapi import FastAPI

from .api import health, sessions, verification, workers
from .capabilities.execution.router import RoutedExecutionProvider
from .capabilities.execution.windows_launcher import WindowsLauncherExecutionProvider
from .capabilities.power.noop import NoopPowerProvider
from .capabilities.power.router import RoutedPowerProvider
from .capabilities.power.wol import WakeOnLanPowerProvider
from .config import Settings, get_settings
from .db import init_db
from .discord_gateway import DiscordGateway
from .drift_monitor import DriftMonitor
from .services.announcement_service import AnnouncementService
from .services.policy_service import PolicyService
from .services.recovery_service import RecoveryService
from .services.verification_service import VerificationService
from .session_service import SessionService
from .thread_manager import ThreadManager
from .transcript_service import TranscriptService
from .worker_registry import WorkerRegistry
from .workflows.pause_workflow import PauseWorkflow
from .workflows.policy_workflow import PolicyWorkflow
from .workflows.start_workflow import StartWorkflow


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
    thread_manager: ThreadManager
    announcement_service: AnnouncementService
    policy_service: PolicyService
    recovery_service: RecoveryService
    verification_service: VerificationService
    session_service: SessionService
    discord_gateway: DiscordGateway
    recovery_loop_task: asyncio.Task[None] | None = None


def build_services(settings: Settings) -> ServiceContainer:
    configure_logging(settings.log_level)
    init_db()
    registry = WorkerRegistry(settings.worker_stale_after_seconds)
    drift_monitor = DriftMonitor()
    transcript_service = TranscriptService()
    thread_manager = ThreadManager(settings)
    announcement_service = AnnouncementService(thread_manager=thread_manager)
    policy_service = PolicyService()
    verification_service = VerificationService(
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
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
    discord_gateway = DiscordGateway(
        settings=settings,
        session_service=session_service,
        verification_service=verification_service,
        registry=registry,
        thread_manager=thread_manager,
    )
    return ServiceContainer(
        settings=settings,
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        announcement_service=announcement_service,
        policy_service=policy_service,
        recovery_service=recovery_service,
        verification_service=verification_service,
        session_service=session_service,
        discord_gateway=discord_gateway,
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
        name="ops-cure-recovery-loop",
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


app = FastAPI(title="Ops-Cure Bridge", lifespan=lifespan)
app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(verification.router)
app.include_router(workers.router)
