from __future__ import annotations

import asyncio
import logging

import discord

from .command_router import register_commands
from .config import Settings
from .message_router import MessageRouter
from .session_service import SessionService
from .thread_manager import ThreadManager
from .worker_registry import WorkerRegistry

LOGGER = logging.getLogger(__name__)


class DiscordBridgeClient(discord.Client):
    def __init__(
        self,
        *,
        settings: Settings,
        session_service: SessionService,
        registry: WorkerRegistry,
        thread_manager: ThreadManager,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = discord.app_commands.CommandTree(self)
        self.message_router = MessageRouter(session_service)
        self._session_service = session_service
        self._registry = registry
        self._thread_manager = thread_manager

    async def setup_hook(self) -> None:
        LOGGER.info("Starting Discord setup hook")
        register_commands(
            self.tree,
            session_service=self._session_service,
            registry=self._registry,
        )
        if self.settings.discord_sync_guild_ids:
            for guild_id in self.settings.discord_sync_guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                LOGGER.info("Syncing Discord commands to guild %s", guild_id)
                await self.tree.sync(guild=guild)
        else:
            LOGGER.info("Syncing global Discord commands")
            await self.tree.sync()
        self._thread_manager.bind_client(self)
        LOGGER.info("Discord setup hook completed")

    async def on_ready(self) -> None:
        LOGGER.info("Discord bridge connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        await self.message_router.handle_message(message)


class DiscordGateway:
    def __init__(
        self,
        *,
        settings: Settings,
        session_service: SessionService,
        registry: WorkerRegistry,
        thread_manager: ThreadManager,
    ) -> None:
        self.settings = settings
        self.session_service = session_service
        self.registry = registry
        self.thread_manager = thread_manager
        self.client: DiscordBridgeClient | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return not self.settings.disable_discord and bool(self.settings.discord_token)

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_ready()

    async def start(self) -> None:
        if not self.enabled:
            LOGGER.warning("Discord gateway disabled.")
            return

        self.client = DiscordBridgeClient(
            settings=self.settings,
            session_service=self.session_service,
            registry=self.registry,
            thread_manager=self.thread_manager,
        )
        self._task = asyncio.create_task(self._run_client(), name="discord-gateway")
        self._task.add_done_callback(self._handle_task_completion)

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run_client(self) -> None:
        assert self.client is not None
        try:
            await self.client.start(self.settings.discord_token)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Discord gateway task crashed")
            raise

    def _handle_task_completion(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            LOGGER.info("Discord gateway task cancelled")
        except Exception:  # noqa: BLE001
            LOGGER.exception("Discord gateway task completed with error")
