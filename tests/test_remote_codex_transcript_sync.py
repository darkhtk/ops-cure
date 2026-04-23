from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from pc_launcher.connectors.remote_executor.device_agent import LocalCodexBackend


class FakeAppServerClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict:
        assert include_turns is True
        return self.payload


def test_local_backend_read_thread_messages_uses_live_turn_messages_when_rollout_is_missing() -> None:
    with TemporaryDirectory() as temp_dir:
        rollout_path = Path(temp_dir) / "missing-rollout.jsonl"
        thread = {
            "id": "thread-1",
            "title": "Thread 1",
            "cwd": r"C:\\Users\\darkh\\Projects\\ops-cure",
            "rolloutPath": str(rollout_path),
            "updatedAtMs": 1700000000000,
            "createdAtMs": 1699999999000,
            "source": "app-server",
            "modelProvider": "openai",
            "model": "gpt-5.4",
            "reasoningEffort": "medium",
            "cliVersion": "1.0.0",
            "firstUserMessage": "existing prompt",
            "status": {"type": "notLoaded"},
            "agentNickname": None,
            "agentRole": None,
        }
        app_server_payload = {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-live",
                        "status": "inProgress",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "live prompt"}],
                            }
                        ],
                    }
                ],
            }
        }
        backend = LocalCodexBackend(
            machine_id="homedev",
            display_name="Home Dev",
            app_server_client=FakeAppServerClient(payload=app_server_payload),
            codex_home=temp_dir,
        )
        backend.get_thread_by_id = lambda thread_id: dict(thread)

        snapshot = backend.read_thread_messages("thread-1", limit=20)

    assert snapshot is not None
    assert [message["text"] for message in snapshot["messages"]] == ["live prompt"]
