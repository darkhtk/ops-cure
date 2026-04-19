from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from .api import health, sessions, workers
from .config import Settings, get_settings
from .db import init_db
from .discord_gateway import DiscordGateway
from .drift_monitor import DriftMonitor
from .session_service import SessionService
from .thread_manager import ThreadManager
from .transcript_service import TranscriptService
from .worker_registry import WorkerRegistry


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
    session_service: SessionService
    discord_gateway: DiscordGateway


def build_services(settings: Settings) -> ServiceContainer:
    configure_logging(settings.log_level)
    init_db()
    registry = WorkerRegistry(settings.worker_stale_after_seconds)
    drift_monitor = DriftMonitor()
    transcript_service = TranscriptService()
    thread_manager = ThreadManager(settings)
    session_service = SessionService(
        registry=registry,
        thread_manager=thread_manager,
        transcript_service=transcript_service,
        drift_monitor=drift_monitor,
    )
    discord_gateway = DiscordGateway(
        settings=settings,
        session_service=session_service,
        registry=registry,
        thread_manager=thread_manager,
    )
    return ServiceContainer(
        settings=settings,
        registry=registry,
        transcript_service=transcript_service,
        thread_manager=thread_manager,
        session_service=session_service,
        discord_gateway=discord_gateway,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    services = build_services(settings)
    app.state.services = services
    await services.discord_gateway.start()
    try:
        yield
    finally:
        await services.discord_gateway.stop()


app = FastAPI(title="Ops-Cure Bridge", lifespan=lifespan)
app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(workers.router)
