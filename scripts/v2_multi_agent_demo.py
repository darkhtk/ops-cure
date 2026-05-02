"""Live multi-agent demo on protocol v2.

Three SDK agents collaborate on an inquiry through a running bridge:

  alice (operator)   opens question, requests review, closes
  claude-pca (worker) replies w/ hypothesis + private note to alice
  claude-pcb (reviewer) challenges with alt hypothesis

Usage:
    BRIDGE_BASE_URL=http://localhost:8080 \\
    BRIDGE_TOKEN=<shared> \\
    DISCORD_THREAD_ID=<existing-thread> \\
    python scripts/v2_multi_agent_demo.py

Each agent is a separate BridgeV2Client instance asserting its own
client_id; the bridge auto-provisions actors_v2 rows and grants the
default kind capabilities. Exits 0 on a clean cycle.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "nas_bridge"))

from app.agent_sdk import AgentRuntime, BridgeV2Client, IncomingEvent  # noqa: E402


def pca_handler(event: IncomingEvent, client: BridgeV2Client) -> None:
    if event.kind != "chat.speech.question":
        return
    client.append_event(
        event.operation_id, kind="speech.claim",
        text="hypothesis: node version mismatch",
    )
    client.append_event(
        event.operation_id, kind="speech.claim",
        text="(private: 100% 확신은 없음)",
        private_to_actors=["alice"],
    )


def pcb_handler(event: IncomingEvent, client: BridgeV2Client) -> None:
    if event.kind != "chat.speech.question":
        return
    client.append_event(
        event.operation_id, kind="speech.claim",
        text="counter: lockfile drift might also explain it",
    )


def main() -> int:
    base = os.environ["BRIDGE_BASE_URL"]
    token = os.environ["BRIDGE_TOKEN"]
    thread_id = os.environ["DISCORD_THREAD_ID"]

    with BridgeV2Client(base_url=base, bearer_token=token, actor_handle="@alice") as alice:
        op = alice.open_operation(
            space_id=thread_id, kind="inquiry",
            title="ci build broke",
            addressed_to="claude-pca",
        )
        op_id = op["id"]
        alice.append_event(
            op_id, kind="speech.question",
            text="why is the build failing?",
            addressed_to="claude-pca",
        )

    with BridgeV2Client(base_url=base, bearer_token=token, actor_handle="@claude-pca") as pca:
        AgentRuntime(pca, pca_handler, poll_interval_seconds=0.0).run_once()

    with BridgeV2Client(base_url=base, bearer_token=token, actor_handle="@alice") as alice:
        alice.append_event(
            op_id, kind="speech.question",
            text="@claude-pcb please review",
            addressed_to="claude-pcb",
        )

    with BridgeV2Client(base_url=base, bearer_token=token, actor_handle="@claude-pcb") as pcb:
        AgentRuntime(pcb, pcb_handler, poll_interval_seconds=0.0).run_once()

    with BridgeV2Client(base_url=base, bearer_token=token, actor_handle="@alice") as alice:
        alice.close_operation(op_id, resolution="answered", summary="two hypotheses received")
        events = alice.list_events(op_id)
        print(f"final event count: {len(events['events'])}")
        for ev in events["events"]:
            text = ev["payload"].get("text", "")[:80]
            print(f"  seq={ev['seq']} kind={ev['kind']} {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
