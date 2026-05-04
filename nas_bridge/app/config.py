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
    # axis H (adversarial robustness): perimeter bounds.
    # Default 1 MiB body cap covers all current ops surface
    # (artifact uploads go through a separate stream path that
    # may need a higher cap). Default depth 32 covers any
    # legitimate metadata / payload._meta nesting we have today.
    # Default 30 s timeout is the slowest live conformance test;
    # SSE prefixes are exempt at the middleware layer.
    # Log-only mode lets staged rollout observe hit rate before
    # turning on enforcement (phase-10 surface-first pattern).
    max_body_bytes: int = Field(
        default=1_048_576,
        alias="BRIDGE_MAX_BODY_BYTES",
    )
    max_json_depth: int = Field(
        default=32,
        alias="BRIDGE_MAX_JSON_DEPTH",
    )
    request_timeout_s: float = Field(
        default=30.0,
        alias="BRIDGE_REQUEST_TIMEOUT_S",
    )
    bounds_log_only: bool = Field(
        default=False,
        alias="BRIDGE_BOUNDS_LOG_ONLY",
    )
    # Phase 12: progression-sweeper. Implicit follow-ups (replies-to,
    # expected_response without explicit addressing) used to silently
    # stall — the sweeper detects them and emits a system.nudge to the
    # inferred responder. Idle threshold is conservative (30s) so a
    # persona LLM call has plenty of time to land before a nudge fires.
    # Two retries cap nudge spam; the third tick on the same trigger
    # escalates to a system DEFER so the op surfaces to the operator.
    progression_nudge_idle_s: float = Field(
        default=30.0,
        alias="BRIDGE_PROGRESSION_NUDGE_IDLE_S",
    )
    progression_nudge_max_retries: int = Field(
        default=2,
        alias="BRIDGE_PROGRESSION_NUDGE_MAX_RETRIES",
    )
    progression_disabled: bool = Field(
        default=False,
        alias="BRIDGE_PROGRESSION_DISABLED",
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

