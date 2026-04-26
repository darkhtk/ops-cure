"""Entry point for the claude_executor agent.

Reads bridge URL + token from env (or flags), constructs a
ClaudeExecutorAgent, runs the polling loop until interrupted.

Required env vars:
  CLAUDE_BRIDGE_URL          (e.g. http://semirain.synology.me:18080)
  CLAUDE_BRIDGE_TOKEN        bearer token shared with the bridge
Optional:
  CLAUDE_BRIDGE_MACHINE_ID   (default: hostname)
  CLAUDE_BRIDGE_DISPLAY_NAME (default: same as machine id)
  CLAUDE_BRIDGE_WORKER_ID    (default: hostname-pid)
  CLAUDE_BRIDGE_POLL_SECONDS (default: 2.0)
  CLAUDE_BRIDGE_SYNC_SECONDS (default: 30.0)
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

from .agent import ClaudeExecutorAgent
from .bridge_client import BridgeClient


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="claude_executor agent for ops-cure remote_claude.")
    p.add_argument("--bridge-url", default=os.getenv("CLAUDE_BRIDGE_URL"),
                   help="Bridge base URL, e.g. http://nas:18080")
    p.add_argument("--token", default=os.getenv("CLAUDE_BRIDGE_TOKEN"),
                   help="Bearer token for the bridge.")
    p.add_argument("--machine-id", default=os.getenv("CLAUDE_BRIDGE_MACHINE_ID") or socket.gethostname().lower())
    p.add_argument("--display-name", default=os.getenv("CLAUDE_BRIDGE_DISPLAY_NAME") or socket.gethostname())
    p.add_argument("--worker-id",
                   default=os.getenv("CLAUDE_BRIDGE_WORKER_ID") or f"{socket.gethostname().lower()}-{os.getpid()}")
    p.add_argument("--poll-seconds", type=float,
                   default=float(os.getenv("CLAUDE_BRIDGE_POLL_SECONDS") or 2.0))
    p.add_argument("--sync-seconds", type=float,
                   default=float(os.getenv("CLAUDE_BRIDGE_SYNC_SECONDS") or 30.0))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.bridge_url or not args.token:
        print("[claude-executor] CLAUDE_BRIDGE_URL and CLAUDE_BRIDGE_TOKEN are required", file=sys.stderr)
        return 2
    bridge = BridgeClient(
        base_url=args.bridge_url,
        token=args.token,
        machine_id=args.machine_id,
        worker_id=args.worker_id,
    )
    agent = ClaudeExecutorAgent(
        bridge=bridge,
        machine_id=args.machine_id,
        display_name=args.display_name,
        sync_interval_seconds=args.sync_seconds,
        poll_interval_seconds=args.poll_seconds,
    )
    print(f"[claude-executor] starting machine={args.machine_id} bridge={args.bridge_url}", file=sys.stderr)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
