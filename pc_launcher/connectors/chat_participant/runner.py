from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from typing import Any
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

try:
    from ...bridge_client import BridgeClient
    from ...config_loader import load_project
    from ...process_io import configure_utf8_stdio
    from .bridge import BridgeChatParticipantClient
    from .connector import ChatParticipantConfig, ChatParticipantConnector
    from .runtime import (
        ChatParticipantRuntime,
        CodexCliChatParticipantRuntime,
        CodexCliRuntimeConfig,
        CodexCurrentThreadChatParticipantRuntime,
        CodexCurrentThreadRuntimeConfig,
    )
    from .state_store import JsonFileChatParticipantStateStore
except ImportError:  # pragma: no cover - script mode support
    from bridge_client import BridgeClient
    from config_loader import load_project
    from pc_launcher.process_io import configure_utf8_stdio
    from pc_launcher.connectors.chat_participant.bridge import BridgeChatParticipantClient
    from pc_launcher.connectors.chat_participant.connector import (
        ChatParticipantConfig,
        ChatParticipantConnector,
    )
    from pc_launcher.connectors.chat_participant.runtime import (
        ChatParticipantRuntime,
        CodexCliChatParticipantRuntime,
        CodexCliRuntimeConfig,
        CodexCurrentThreadChatParticipantRuntime,
        CodexCurrentThreadRuntimeConfig,
    )
    from pc_launcher.connectors.chat_participant.state_store import JsonFileChatParticipantStateStore


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RunnerConfig:
    project_file: Path
    thread_id: str
    actor_name: str
    actor_kind: str
    machine_label: str | None
    workdir: str | None
    state_file: Path
    runtime_mode: str
    codex_thread_id: str | None
    allow_unprompted: bool
    delta_limit: int
    poll_interval_seconds: float
    run_once: bool


def default_state_file() -> Path:
    root = Path(os.getenv("LOCALAPPDATA") or Path.home())
    return root / "OpsCure" / "chat_participant_state.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local Codex chat participant against the Opscure chat behavior.",
    )
    parser.add_argument(
        "--project-file",
        default=str(Path(__file__).resolve().parents[2] / "projects" / "sample" / "project.yaml"),
        help="Path to the pc_launcher project.yaml file that defines the bridge connection.",
    )
    parser.add_argument("--thread-id", required=True, help="Chat room thread id to attach to.")
    parser.add_argument("--actor-name", required=True, help="Participant name to register.")
    parser.add_argument("--actor-kind", default="ai", help="Participant kind to register.")
    parser.add_argument(
        "--machine-label",
        default=socket.gethostname(),
        help="Optional machine label exposed to the local runtime prompt.",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Optional working directory for the local Codex runtime. Defaults to the project workdir.",
    )
    parser.add_argument(
        "--state-file",
        default=str(default_state_file()),
        help="JSON file that stores the last seen message cursor for this connector.",
    )
    parser.add_argument(
        "--runtime-mode",
        choices=["auto", "cli", "current-thread"],
        default=os.getenv("CHAT_PARTICIPANT_RUNTIME_MODE", "auto"),
        help="Runtime backend for generating replies.",
    )
    parser.add_argument(
        "--codex-thread-id",
        default=os.getenv("CHAT_PARTICIPANT_CODEX_THREAD_ID") or os.getenv("CODEX_THREAD_ID"),
        help="Existing Codex thread id to use when runtime-mode is current-thread.",
    )
    parser.add_argument(
        "--allow-unprompted",
        action="store_true",
        help="Allow replies even when the latest message does not explicitly target this actor.",
    )
    parser.add_argument(
        "--targeted-only",
        dest="allow_unprompted",
        action="store_false",
        help="Require explicit targeting such as @actor_name before replying.",
    )
    parser.set_defaults(
        allow_unprompted=os.getenv("CHAT_PARTICIPANT_ALLOW_UNPROMPTED", "true").lower() != "false",
    )
    parser.add_argument("--delta-limit", type=int, default=20, help="Unread message batch size.")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=10.0,
        help="Reconnect backoff for stream mode and retry interval for one-shot failures.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle instead of polling forever.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> RunnerConfig:
    args = build_parser().parse_args(argv)
    return RunnerConfig(
        project_file=Path(args.project_file).resolve(),
        thread_id=args.thread_id,
        actor_name=args.actor_name,
        actor_kind=args.actor_kind,
        machine_label=args.machine_label,
        workdir=args.workdir,
        state_file=Path(args.state_file).resolve(),
        runtime_mode=str(args.runtime_mode),
        codex_thread_id=args.codex_thread_id or None,
        allow_unprompted=bool(args.allow_unprompted),
        delta_limit=max(1, int(args.delta_limit)),
        poll_interval_seconds=max(1.0, float(args.poll_seconds)),
        run_once=bool(args.once),
    )


def resolve_runtime_mode(config: RunnerConfig) -> str:
    if config.runtime_mode == "auto":
        return "current-thread" if config.codex_thread_id else "cli"
    return config.runtime_mode


def build_runtime(*, config: RunnerConfig, default_workdir: str | None) -> ChatParticipantRuntime:
    runtime_mode = resolve_runtime_mode(config)
    runtime_cwd = config.workdir or default_workdir
    if runtime_mode == "current-thread":
        if not config.codex_thread_id:
            raise RuntimeError("current-thread runtime requires --codex-thread-id or CODEX_THREAD_ID.")
        return CodexCurrentThreadChatParticipantRuntime(
            config=CodexCurrentThreadRuntimeConfig.from_env(
                cwd=runtime_cwd,
                thread_id=config.codex_thread_id,
            ),
        )
    return CodexCliChatParticipantRuntime(
        config=CodexCliRuntimeConfig.from_env(cwd=runtime_cwd),
    )


def build_connector(config: RunnerConfig) -> ChatParticipantConnector:
    load_dotenv(config.project_file.parents[2] / ".env")
    project = load_project(config.project_file)
    auth_token = os.environ[project.bridge.auth_token_env]
    bridge_client = BridgeClient(
        base_url=project.bridge.base_url,
        auth_token=auth_token,
    )
    bridge = BridgeChatParticipantClient(bridge_client=bridge_client)
    runtime = build_runtime(config=config, default_workdir=project.default_workdir)
    state_store = JsonFileChatParticipantStateStore(path=config.state_file)
    participant_config = ChatParticipantConfig(
        actor_name=config.actor_name,
        actor_kind=config.actor_kind,
        machine_label=config.machine_label,
        default_thread_id=config.thread_id,
        allow_unprompted=config.allow_unprompted,
        delta_limit=config.delta_limit,
    )
    return ChatParticipantConnector(
        bridge=bridge,
        runtime=runtime,
        state_store=state_store,
        config=participant_config,
    )


def log_sync_result(result) -> None:
    LOGGER.info(
        "Chat participant sync status=%s reason=%s thread=%s replied=%s seen=%s",
        result.status,
        result.reason,
        result.thread_id,
        result.replied_message_id,
        result.seen_message_id,
    )


def latest_event_cursor(bridge: BridgeChatParticipantClient, *, thread_id: str) -> str | None:
    snapshot = bridge.get_events_for_thread(thread_id=thread_id, limit=1, kinds=["message"])
    return snapshot.get("next_cursor")


def run_stream_session(connector: ChatParticipantConnector, *, thread_id: str, reconnect_seconds: float) -> None:
    actor_name = connector.config.actor_name
    connector.bridge.register_chat_participant(
        thread_id=thread_id,
        actor_name=actor_name,
        actor_kind=connector.config.actor_kind,
    )
    after_cursor = connector.state_store.get_event_cursor(actor_name=actor_name, thread_id=thread_id)
    initial_catchup_required = after_cursor is None
    stream = connector.bridge.stream_events_for_thread(
        thread_id=thread_id,
        after_cursor=after_cursor,
        limit=max(100, connector.config.delta_limit),
        kinds=["message"],
        subscriber_id=actor_name,
    )

    for event_name, payload in stream:
        if event_name == "open":
            accepted_after = payload.get("accepted_after_cursor")
            latest_cursor = payload.get("latest_cursor")
            LOGGER.info(
                "Chat participant stream open thread=%s accepted_after=%s latest=%s",
                thread_id,
                accepted_after or "(none)",
                latest_cursor or "(none)",
            )
            connector.bridge.heartbeat_chat_participant(thread_id=thread_id, actor_name=actor_name)
            if initial_catchup_required:
                initial_result = connector.sync_once(thread_id=thread_id)
                log_sync_result(initial_result)
                if latest_cursor:
                    connector.state_store.set_event_cursor(
                        actor_name=actor_name,
                        thread_id=thread_id,
                        event_cursor=str(latest_cursor),
                    )
                initial_catchup_required = False
            continue

        if event_name == "heartbeat":
            connector.bridge.heartbeat_chat_participant(thread_id=thread_id, actor_name=actor_name)
            continue

        if event_name == "reset":
            LOGGER.warning(
                "Chat participant stream reset thread=%s reason=%s; rebuilding cursor.",
                thread_id,
                payload.get("reason") or "unknown",
            )
            recovery_result = connector.sync_once(thread_id=thread_id)
            log_sync_result(recovery_result)
            refreshed_cursor = latest_event_cursor(connector.bridge, thread_id=thread_id)
            if refreshed_cursor:
                connector.state_store.set_event_cursor(
                    actor_name=actor_name,
                    thread_id=thread_id,
                    event_cursor=refreshed_cursor,
                )
            time.sleep(reconnect_seconds)
            return

        if event_name != "event":
            LOGGER.debug("Ignoring chat participant stream event type=%s payload=%s", event_name, payload)
            continue

        event = payload.get("event") or {}
        event_cursor = payload.get("cursor")
        LOGGER.info(
            "Chat participant received room event thread=%s cursor=%s actor=%s kind=%s",
            thread_id,
            event_cursor or "(none)",
            event.get("actor_name") or "(unknown)",
            event.get("kind") or "(unknown)",
        )
        result = connector.sync_once(thread_id=thread_id)
        log_sync_result(result)
        if event_cursor:
            connector.state_store.set_event_cursor(
                actor_name=actor_name,
                thread_id=thread_id,
                event_cursor=str(event_cursor),
            )


def run(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    connector = build_connector(config)
    LOGGER.info(
        "Chat participant runtime=%s actor=%s codex_thread_id=%s",
        resolve_runtime_mode(config),
        config.actor_name,
        config.codex_thread_id or "(none)",
    )

    if config.run_once:
        result = connector.sync_once(thread_id=config.thread_id)
        log_sync_result(result)
        return 0

    while True:
        try:
            run_stream_session(
                connector,
                thread_id=config.thread_id,
                reconnect_seconds=config.poll_interval_seconds,
            )
        except Exception:
            LOGGER.exception(
                "Chat participant stream failed for actor=%s thread=%s; reconnecting after backoff.",
                config.actor_name,
                config.thread_id,
            )
        time.sleep(config.poll_interval_seconds)


def main() -> int:
    configure_utf8_stdio()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return run()
    except KeyboardInterrupt:
        LOGGER.info("Chat participant runner interrupted.")
        return 130


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    raise SystemExit(main())
