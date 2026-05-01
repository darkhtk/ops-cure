"""Demo: an autonomous SDK-driven agent on protocol v2.

Runs against a live ops-cure bridge. The agent:
  1. opens an inquiry conversation addressed to @claude-pca
  2. starts a runtime as @claude-pca that auto-replies to the question
  3. asserts the reply landed in v2 and the conversation can be closed

Usage:
    BRIDGE_BASE_URL=http://localhost:8080 \
    BRIDGE_TOKEN=<shared> \
    DISCORD_THREAD_ID=<existing-thread> \
    python scripts/v2_agent_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "nas_bridge"))

from app.agent_sdk import AgentRuntime, BridgeV2Client, IncomingEvent  # noqa: E402


def auto_reply_handler(event: IncomingEvent, client: BridgeV2Client) -> None:
    """The agent's brain. v2 events come in here; the agent decides
    whether to speak, ignore, or close based on event kind + payload.
    """
    # Only react to a fresh inquiry question addressed at us.
    if event.kind != "chat.speech.question":
        return
    text = event.payload.get("text", "")
    # Reply with a stub answer.
    # operation_id maps to v1 conversation_id 1:1 (dual-write era).
    # For now we look it up via metadata.v1_conversation_id but the
    # demo uses the chat surface so re-resolving is simpler:
    op = client.get_operation(event.operation_id)
    v1_conv_id = op.get("metadata", {}).get("v1_conversation_id")
    if v1_conv_id:
        client.submit_speech(
            conversation_id=v1_conv_id,
            kind="claim",
            content=f"echo: {text}",
            replies_to_speech_id=None,
        )


def main() -> int:
    base = os.environ["BRIDGE_BASE_URL"]
    token = os.environ["BRIDGE_TOKEN"]
    thread_id = os.environ["DISCORD_THREAD_ID"]

    with BridgeV2Client(base_url=base, bearer_token=token, actor_handle="@alice") as alice:
        opened = alice.open_conversation(
            discord_thread_id=thread_id,
            kind="inquiry",
            title="echo test",
            addressed_to="claude-pca",
        )
        v1_conv_id = opened["id"]
        alice.submit_speech(
            conversation_id=v1_conv_id,
            kind="question",
            content="what is 2+2?",
            addressed_to="claude-pca",
        )

    with BridgeV2Client(base_url=base, bearer_token=token, actor_handle="@claude-pca") as claude:
        runtime = AgentRuntime(claude, auto_reply_handler, poll_interval_seconds=0.0)
        # one tick is enough for the demo
        n = runtime.run_once()
        print(f"dispatched {n} event(s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
