from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

try:
    from ...bridge_client import BridgeClient
    from ...config_loader import load_project
    from ...process_io import configure_utf8_stdio
except ImportError:  # pragma: no cover - script mode support
    from bridge_client import BridgeClient
    from config_loader import load_project
    from pc_launcher.process_io import configure_utf8_stdio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit a chat participant message through the bridge using a UTF-8 safe path.",
    )
    parser.add_argument(
        "--project-file",
        default=str(Path(__file__).resolve().parents[2] / "projects" / "sample" / "project.yaml"),
        help="Path to the pc_launcher project.yaml file that defines the bridge connection.",
    )
    parser.add_argument("--thread-id", required=True, help="Chat room thread id to post into.")
    parser.add_argument("--actor-name", required=True, help="Participant name to use for the message.")
    parser.add_argument("--actor-kind", default="ai", help="Participant kind to report to the bridge.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--message", help="Inline message text.")
    source_group.add_argument(
        "--message-file",
        help="UTF-8 text file containing the message body. Recommended for non-ASCII text on Windows.",
    )
    source_group.add_argument(
        "--stdin",
        action="store_true",
        help="Read the message body from standard input using UTF-8.",
    )
    return parser


def resolve_message(*, inline_message: str | None, message_file: str | None, read_stdin: bool) -> str:
    if inline_message is not None:
        return inline_message
    if message_file is not None:
        return Path(message_file).read_text(encoding="utf-8").strip()
    if read_stdin:
        import sys

        return sys.stdin.read().strip()
    raise RuntimeError("No message source was provided.")


def run(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    project_file = Path(args.project_file).resolve()
    load_dotenv(project_file.parents[2] / ".env")
    project = load_project(project_file)
    auth_token = os.environ[project.bridge.auth_token_env]
    message = resolve_message(
        inline_message=args.message,
        message_file=args.message_file,
        read_stdin=bool(args.stdin),
    )
    client = BridgeClient(base_url=project.bridge.base_url, auth_token=auth_token)
    response = client.submit_chat_message(
        thread_id=args.thread_id,
        actor_name=args.actor_name,
        actor_kind=args.actor_kind,
        content=message,
    )
    print(response["message"]["content"])
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    raise SystemExit(main())
