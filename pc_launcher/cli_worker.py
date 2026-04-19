from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

try:
    from .worker_runtime import WorkerRuntime
except ImportError:  # pragma: no cover - script mode support
    from worker_runtime import WorkerRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a single bridge-connected CLI worker.")
    parser.add_argument("--project-file", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--launcher-id", required=True)
    parser.add_argument("--workdir-override", default=None)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--heartbeat-interval", type=int, default=15)
    parser.add_argument("--poll-interval", type=int, default=3)
    return parser


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    runtime = WorkerRuntime(
        project_file=args.project_file,
        session_id=args.session_id,
        agent_name=args.agent_name,
        launcher_id=args.launcher_id,
        workdir_override=args.workdir_override,
        worker_id=args.worker_id,
        heartbeat_interval_seconds=args.heartbeat_interval,
        poll_interval_seconds=args.poll_interval,
    )
    runtime.run_forever()


if __name__ == "__main__":
    main()
