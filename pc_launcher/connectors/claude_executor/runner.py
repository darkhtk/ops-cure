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

External-agent mode (kernel + external agent architecture):
  CLAUDE_BRIDGE_ACTOR_HANDLE        when set (e.g. "@bridge-agent"), the
                                    executor subscribes to the v2 inbox
                                    SSE for that actor and runs claude on
                                    each addressed event, posting the
                                    result back as a speech.claim.
  CLAUDE_BRIDGE_AGENT_CWD           cwd for agent-mode claude runs.
  CLAUDE_BRIDGE_AGENT_MODEL         override model (e.g. claude-opus-4-7)
  CLAUDE_BRIDGE_AGENT_PERMISSION    permission mode (default: acceptEdits)
  CLAUDE_BRIDGE_AGENT_BROADCAST     "1"/"true" -> also respond to events
                                    with no specific addressed_to (room-
                                    wide speech). Default off.
  CLAUDE_BRIDGE_AGENT_HISTORY_LIMIT int; pre-fetch last N op events into
                                    the prompt for context. 0 = off.
  CLAUDE_BRIDGE_AGENT_MAX_PER_OP    int; cap replies per op (loop guard
                                    when broadcast is on). Default 5.
  CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT persona-specific system text prepended
                                    to every prompt — lets one binary host
                                    different personas via env.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

from .agent import ClaudeExecutorAgent
from .agent_loop import BridgeAgentLoop
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

    actor_handle = os.getenv("CLAUDE_BRIDGE_ACTOR_HANDLE", "").strip()
    agent_loop: BridgeAgentLoop | None = None
    if actor_handle:
        agent_cwd = os.getenv("CLAUDE_BRIDGE_AGENT_CWD", "").strip() or os.getcwd()
        agent_model = os.getenv("CLAUDE_BRIDGE_AGENT_MODEL", "").strip() or None
        agent_permission = (
            os.getenv("CLAUDE_BRIDGE_AGENT_PERMISSION", "").strip() or "acceptEdits"
        )
        broadcast = (
            os.getenv("CLAUDE_BRIDGE_AGENT_BROADCAST", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        try:
            history_limit = int(os.getenv("CLAUDE_BRIDGE_AGENT_HISTORY_LIMIT", "0"))
        except ValueError:
            history_limit = 0
        try:
            max_per_op = int(os.getenv("CLAUDE_BRIDGE_AGENT_MAX_PER_OP", "5"))
        except ValueError:
            max_per_op = 5
        system_prompt = os.getenv("CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT", "").strip() or None
        agent_loop = BridgeAgentLoop(
            bridge_url=args.bridge_url,
            token=args.token,
            actor_handle=actor_handle,
            cwd=agent_cwd,
            model=agent_model,
            permission_mode=agent_permission,
            broadcast=broadcast,
            history_limit=history_limit,
            max_responses_per_op=max_per_op,
            system_prompt=system_prompt,
        )
        agent_loop.start()

    print(f"[claude-executor] starting machine={args.machine_id} bridge={args.bridge_url}", file=sys.stderr)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
        if agent_loop is not None:
            agent_loop.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
