from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "discord-cli-bridge"
    environment: str = Field(default="development", alias="BRIDGE_ENV")
    log_level: str = Field(default="INFO", alias="BRIDGE_LOG_LEVEL")
    host: str = Field(default="0.0.0.0", alias="BRIDGE_HOST")
    port: int = Field(default=8080, alias="BRIDGE_PORT")
    database_url: str = Field(
        default="sqlite:///./data/bridge.db",
        alias="BRIDGE_DATABASE_URL",
    )
    shared_auth_token: str = Field(alias="BRIDGE_SHARED_AUTH_TOKEN")
    disable_discord: bool = Field(default=False, alias="BRIDGE_DISABLE_DISCORD")
    discord_token: str | None = Field(default=None, alias="DISCORD_BOT_TOKEN")
    discord_application_id: int | None = Field(
        default=None,
        alias="DISCORD_APPLICATION_ID",
    )
    discord_sync_guild_ids: list[int] = Field(
        default_factory=list,
        alias="DISCORD_SYNC_GUILD_IDS",
    )
    worker_stale_after_seconds: int = Field(
        default=90,
        alias="BRIDGE_WORKER_STALE_AFTER_SECONDS",
    )
    recovery_loop_interval_seconds: float = Field(
        default=5.0,
        alias="BRIDGE_RECOVERY_LOOP_INTERVAL_SECONDS",
    )
    stalled_start_timeout_seconds: int = Field(
        default=300,
        alias="BRIDGE_STALLED_START_TIMEOUT_SECONDS",
    )
    # F8: v1 chat surface deprecation. The legacy chat_* endpoints stay
    # functional through the dual-write era. When this flag is true the
    # bridge logs a one-time deprecation banner at startup so operators
    # know to migrate clients to /v2/operations + /v2/inbox.
    chat_v1_deprecation_warning: bool = Field(
        default=True,
        alias="BRIDGE_CHAT_V1_DEPRECATION_WARNING",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def sqlite_path(self) -> Path:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Only sqlite:/// URLs are supported by this MVP.")
        raw_path = self.database_url.removeprefix("sqlite:///")
        return Path(raw_path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

