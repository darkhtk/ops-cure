from __future__ import annotations

import asyncio


def test_machine_subscription_receives_thread_command_events(app_env) -> None:
    from app.behaviors.remote_codex.state_service import RemoteCodexStateService

    service = RemoteCodexStateService()
    handle = service.subscribe_machine("machine-live")
    try:
        payload = {
            "kind": "command",
            "command": {
                "commandId": "cmd-1",
                "threadId": "thread-live",
                "type": "turn.start",
                "status": "queued",
            },
        }
        service._publish("machine-live", "thread-live", payload)
        received = asyncio.run(asyncio.wait_for(handle.queue.get(), timeout=0.1))
        assert received == payload
    finally:
        handle.unsubscribe()
