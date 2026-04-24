from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from pc_launcher.connectors.remote_executor.device_agent import (
    LocalCodexBackend,
    RemoteCodexDeviceAgent,
    WindowsCodexDesktopPromptSubmitter,
    build_thread_version,
    merge_missing_turn_messages,
    merge_adjacent_message,
    normalize_rollout_message,
    normalize_turn_item_message,
)


@dataclass
class FakeBackend:
    threads: list[dict]
    snapshots: dict[str, dict]
    health: dict[str, object]
    read_limits: list[tuple[str, int]] = field(default_factory=list)
    start_turn_calls: list[tuple[str, str]] = field(default_factory=list)
    materialized_prompts: list[tuple[str, str, str]] = field(default_factory=list)
    desktop_ui_submit_calls: list[tuple[str, str]] = field(default_factory=list)
    desktop_ui_enabled_threads: set[str] = field(default_factory=set)
    desktop_ui_submit_mode: str | None = "desktop-ipc"
    interrupt_calls: list[tuple[str, str]] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def get_health(self) -> dict[str, object]:
        return dict(self.health)

    def list_threads(self, *, limit: int = 60, query: str = "") -> list[dict]:
        return list(self.threads)[:limit]

    def get_thread_by_id(self, thread_id: str) -> dict | None:
        for thread in self.threads:
            if thread["id"] == thread_id:
                return dict(thread)
        return None

    def read_thread_messages(self, thread_id: str, *, limit: int = 300) -> dict | None:
        self.read_limits.append((thread_id, limit))
        snapshot = self.snapshots.get(thread_id)
        return dict(snapshot) if snapshot is not None else None

    def start_turn(self, thread_id: str, prompt: str) -> dict:
        self.actions.append("start_turn")
        self.start_turn_calls.append((thread_id, prompt))
        return {
            "turn": {
                "id": "turn-1",
                "status": "inProgress",
            }
        }

    def materialize_pending_browser_prompt(self, *, thread_id: str, command_id: str, prompt: str) -> bool:
        self.actions.append("materialize")
        self.materialized_prompts.append((thread_id, command_id, prompt))
        return True

    def can_submit_prompt_via_desktop_ui(self, thread_id: str) -> bool:
        return thread_id in self.desktop_ui_enabled_threads

    def submit_prompt_via_desktop_ui(self, *, thread_id: str, prompt: str) -> str | None:
        if not self.can_submit_prompt_via_desktop_ui(thread_id):
            return None
        self.actions.append("desktop_submit")
        self.desktop_ui_submit_calls.append((thread_id, prompt))
        return self.desktop_ui_submit_mode

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict:
        self.interrupt_calls.append((thread_id, turn_id))
        return {"ok": True}

    def delete_thread(self, thread_id: str) -> dict:
        self.delete_calls.append(thread_id)
        self.threads = [thread for thread in self.threads if thread["id"] != thread_id]
        self.snapshots.pop(thread_id, None)
        return {"threadId": thread_id, "archived": True}


@dataclass
class FakeBridge:
    queued_commands: list[dict] = field(default_factory=list)
    thread_commands_by_thread: dict[str, list[dict]] = field(default_factory=dict)
    sync_calls: list[dict] = field(default_factory=list)
    command_result_calls: list[dict] = field(default_factory=list)
    heartbeat_calls: list[dict] = field(default_factory=list)
    evidence_calls: list[dict] = field(default_factory=list)
    complete_calls: list[dict] = field(default_factory=list)
    fail_calls: list[dict] = field(default_factory=list)

    def sync_remote_codex_agent(self, *, machine: dict, threads: list[dict], snapshots: list[dict]) -> dict:
        self.sync_calls.append(
            {
                "machine": machine,
                "threads": list(threads),
                "snapshots": list(snapshots),
            }
        )
        return {"ok": True}

    def claim_next_remote_codex_command(self, *, machine_id: str, worker_id: str) -> dict | None:
        return self.queued_commands.pop(0) if self.queued_commands else None

    def get_remote_codex_thread_commands(
        self,
        *,
        machine_id: str,
        thread_id: str,
        limit: int = 8,
    ) -> list[dict]:
        return list(self.thread_commands_by_thread.get(thread_id, []))[:limit]

    def report_remote_codex_command_result(
        self,
        *,
        command_id: str,
        worker_id: str,
        status: str,
        result: dict | None = None,
        error: dict | None = None,
    ) -> dict:
        payload = {
            "command_id": command_id,
            "worker_id": worker_id,
            "status": status,
            "result": result,
            "error": error,
        }
        self.command_result_calls.append(payload)
        return {"ok": True}

    def heartbeat_remote_codex_agent_task(
        self,
        *,
        task_id: str,
        actor_id: str,
        phase: str,
        summary: str,
        commands_run_count: int = 0,
        files_read_count: int = 0,
        files_modified_count: int = 0,
        tests_run_count: int = 0,
    ) -> dict:
        payload = {
            "task_id": task_id,
            "actor_id": actor_id,
            "phase": phase,
            "summary": summary,
            "commands_run_count": commands_run_count,
            "files_read_count": files_read_count,
            "files_modified_count": files_modified_count,
            "tests_run_count": tests_run_count,
        }
        self.heartbeat_calls.append(payload)
        return {"ok": True}

    def add_remote_codex_agent_task_evidence(
        self,
        *,
        task_id: str,
        actor_id: str,
        kind: str,
        summary: str,
        payload: dict | None = None,
    ) -> dict:
        next_payload = {
            "task_id": task_id,
            "actor_id": actor_id,
            "kind": kind,
            "summary": summary,
            "payload": payload or {},
        }
        self.evidence_calls.append(next_payload)
        return {"ok": True}

    def complete_remote_codex_agent_task(self, *, task_id: str, actor_id: str, summary: str) -> dict:
        self.complete_calls.append(
            {
                "task_id": task_id,
                "actor_id": actor_id,
                "summary": summary,
            }
        )
        return {"ok": True}

    def fail_remote_codex_agent_task(self, *, task_id: str, actor_id: str, error_text: str) -> dict:
        self.fail_calls.append(
            {
                "task_id": task_id,
                "actor_id": actor_id,
                "error_text": error_text,
            }
        )
        return {"ok": True}


@dataclass
class FakeAppServerClient:
    payload: dict

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict:
        assert include_turns is True
        return self.payload

    def list_threads(self, *, limit: int = 60) -> dict:
        return {"threads": []}

    def resume_thread(self, thread_id: str) -> dict:
        return {"ok": True}

    def start_turn(self, thread_id: str, prompt: str) -> dict:
        return {"turn": {"id": "turn-1", "status": "inProgress"}}

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict:
        return {"ok": True}

    def wait_for_turn_completion(self, *, thread_id: str, turn_id: str, timeout_seconds: float) -> tuple[dict, str]:
        raise NotImplementedError

    def close(self) -> None:
        return None


def _sample_thread() -> dict:
    return {
        "id": "thread-1",
        "title": "Remote Codex Thread",
        "cwd": r"C:\Users\darkh\Projects\ops-cure",
        "rolloutPath": r"C:\Users\darkh\.codex\rollout.jsonl",
        "updatedAtMs": 1700000000000,
        "createdAtMs": 1699999999000,
        "source": "app-server",
        "modelProvider": "openai",
        "model": "gpt-5.4",
        "reasoningEffort": "medium",
        "cliVersion": "1.0.0",
        "firstUserMessage": "Ship this remote task flow.",
        "status": {"type": "notLoaded"},
        "agentNickname": None,
        "agentRole": None,
    }


def _sample_snapshot() -> dict:
    thread = _sample_thread()
    return {
        "thread": thread,
        "messages": [
            {
                "lineNumber": 1,
                "timestamp": "2026-04-23T00:00:00+00:00",
                "role": "user",
                "phase": None,
                "text": "Ship this remote task flow.",
                "images": [],
            }
        ],
        "totalMessages": 1,
        "lineCount": 1,
        "fileSize": 64,
    }


def test_normalize_rollout_message_extracts_uploaded_images_and_filters_placeholder_text() -> None:
    entry = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Look at this screenshot.\n"},
                {"type": "input_text", "text": "<image>"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,abc123",
                    "title": "Screenshot",
                },
            ],
        },
    }

    message = normalize_rollout_message(entry, line_number=7)

    assert message is not None
    assert message["text"] == "Look at this screenshot."
    assert message["images"] == [
        {
            "src": "data:image/png;base64,abc123",
            "alt": "Screenshot",
            "title": "Screenshot",
        }
    ]


def test_merge_adjacent_message_keeps_image_payload_from_duplicate_message() -> None:
    previous = {
        "lineNumber": 1,
        "timestamp": "2026-04-23T00:00:00+00:00",
        "role": "user",
        "phase": None,
        "text": "Look at this screenshot.",
        "images": [],
    }
    current = {
        "lineNumber": 2,
        "timestamp": "2026-04-23T00:00:01+00:00",
        "role": "user",
        "phase": None,
        "text": "Look at this screenshot.",
        "images": [
            {
                "src": "data:image/png;base64,abc123",
                "alt": "Uploaded image 1",
                "title": None,
            }
        ],
    }

    merged = merge_adjacent_message(previous, current)

    assert merged is True
    assert previous["images"] == current["images"]
    assert previous["lineNumber"] == 2


def test_merge_adjacent_message_collapses_whitespace_only_text_differences() -> None:
    previous = {
        "lineNumber": 10,
        "timestamp": "2026-04-23T00:00:00+00:00",
        "role": "assistant",
        "phase": "final_answer",
        "text": "Hello.\n\nThis is a test.",
        "images": [],
    }
    current = {
        "lineNumber": 11,
        "timestamp": "2026-04-23T00:00:01+00:00",
        "role": "assistant",
        "phase": "final_answer",
        "text": "Hello. This is a test.",
        "images": [],
    }

    merged = merge_adjacent_message(previous, current)

    assert merged is True
    assert previous["lineNumber"] == 11
    assert previous["timestamp"] == "2026-04-23T00:00:01+00:00"


def test_normalize_turn_item_message_extracts_user_text_from_turn_content() -> None:
    item = {
        "type": "userMessage",
        "content": [
            {
                "type": "text",
                "text": "컴포저 높이가 너무 커.",
            }
        ],
    }

    message = normalize_turn_item_message(item, sequence_number=10, phase="inProgress")

    assert message is not None
    assert message["role"] == "user"
    assert message["text"] == "컴포저 높이가 너무 커."
    assert message["images"] == []


def test_merge_missing_turn_messages_appends_recent_prompt_not_in_rollout_tail() -> None:
    rollout_messages = [
        {
            "lineNumber": 1,
            "timestamp": "2026-04-23T00:00:00+00:00",
            "role": "user",
            "phase": None,
            "text": "기존 프롬프트",
            "images": [],
        }
    ]
    turn_messages = [
        {
            "lineNumber": 1_000_000,
            "timestamp": None,
            "role": "user",
            "phase": None,
            "text": "컴포저 높이가 너무 커.",
            "images": [],
        }
    ]

    merged = merge_missing_turn_messages(rollout_messages, turn_messages, recent_window=20)

    assert [message["text"] for message in merged] == [
        "기존 프롬프트",
        "컴포저 높이가 너무 커.",
    ]


def test_local_backend_read_thread_messages_prefers_rollout_transcript_when_rollout_exists() -> None:
    with TemporaryDirectory() as temp_dir:
        rollout_path = Path(temp_dir) / "rollout.jsonl"
        rollout_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"기존 프롬프트"}}\n',
            encoding="utf-8",
        )
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
            "firstUserMessage": "기존 프롬프트",
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
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "컴포저 높이가 너무 커.",
                                    }
                                ],
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
    assert len(snapshot["messages"]) == 1
    dummy = [
        "기존 프롬프트",
        "컴포저 높이가 너무 커.",
    ]


def test_local_backend_materializes_pending_browser_prompt_into_rollout() -> None:
    with TemporaryDirectory() as temp_dir:
        rollout_path = Path(temp_dir) / "rollout.jsonl"
        state_db_path = Path(temp_dir) / "state_5.sqlite"
        rollout_path.write_text(
            '{"type":"event_msg","payload":{"type":"agent_message","message":"existing answer"}}\n',
            encoding="utf-8",
        )
        with closing(sqlite3.connect(state_db_path)) as connection:
            connection.execute(
                """
                create table threads (
                    id text primary key,
                    title text,
                    cwd text,
                    rollout_path text,
                    updated_at_ms integer,
                    created_at_ms integer,
                    source text,
                    model_provider text,
                    model text,
                    reasoning_effort text,
                    cli_version text,
                    first_user_message text,
                    archived integer default 0
                )
                """
            )
            connection.execute(
                """
                insert into threads (
                    id, title, cwd, rollout_path, updated_at_ms, created_at_ms,
                    source, model_provider, model, reasoning_effort, cli_version, first_user_message, archived
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    "thread-1",
                    "Thread 1",
                    r"C:\\Users\\darkh\\Projects\\ops-cure",
                    str(rollout_path),
                    1700000000000,
                    1699999999000,
                    "app-server",
                    "openai",
                    "gpt-5.4",
                    "medium",
                    "1.0.0",
                    "existing answer",
                ),
            )
            connection.commit()
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
            "firstUserMessage": "existing answer",
            "status": {"type": "inProgress"},
            "agentNickname": None,
            "agentRole": None,
        }
        backend = LocalCodexBackend(
            machine_id="homedev",
            display_name="Home Dev",
            app_server_client=FakeAppServerClient(payload={"thread": {"id": "thread-1", "turns": []}}),
            codex_home=temp_dir,
        )
        backend.get_thread_by_id = lambda thread_id: dict(thread)

        created = backend.materialize_pending_browser_prompt(
            thread_id="thread-1",
            command_id="command-1",
            prompt="[TEST]hello",
        )
        duplicate = backend.materialize_pending_browser_prompt(
            thread_id="thread-1",
            command_id="command-1",
            prompt="[TEST]hello",
        )
        snapshot = backend.read_thread_messages("thread-1", limit=20)
        rollout_entries = [
            json.loads(line)
            for line in rollout_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        registry_payload = json.loads((Path(temp_dir) / "remote_codex_pending_rollout.json").read_text(encoding="utf-8"))
        with closing(sqlite3.connect(state_db_path)) as connection:
            updated_at_ms = connection.execute(
                "select updated_at_ms from threads where id = ?",
                ("thread-1",),
            ).fetchone()[0]

    assert created is True
    assert duplicate is False
    assert snapshot is not None
    assert [message["text"] for message in snapshot["messages"]] == [
        "existing answer",
        "[TEST]hello",
    ]
    assert [entry["type"] for entry in rollout_entries[-2:]] == ["response_item", "event_msg"]
    assert rollout_entries[-2]["payload"]["role"] == "user"
    assert rollout_entries[-1]["payload"]["type"] == "user_message"
    assert rollout_entries[-2]["payload"]["content"][0]["text"] == "[TEST]hello"
    assert rollout_entries[-1]["payload"]["message"] == "[TEST]hello"
    assert registry_payload == {
        "thread-1": [
            {
                "timestamp": rollout_entries[-2]["timestamp"],
                "commandId": "command-1",
                "text": "[TEST]hello",
            }
        ]
    }
    assert int(updated_at_ms) > 1700000000000


def test_local_backend_reconciles_pending_browser_prompt_after_actual_user_message_arrives() -> None:
    with TemporaryDirectory() as temp_dir:
        rollout_path = Path(temp_dir) / "rollout.jsonl"
        rollout_path.write_text(
            '{"type":"response_item","timestamp":"2026-04-24T00:00:00+00:00","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"[TEST]hello"}]}}\n'
            '{"type":"event_msg","timestamp":"2026-04-24T00:00:00+00:00","payload":{"type":"user_message","message":"[TEST]hello"}}\n'
            '{"type":"event_msg","timestamp":"2026-04-24T00:00:10+00:00","payload":{"type":"user_message","message":"[TEST]hello"}}\n',
            encoding="utf-8",
        )
        (Path(temp_dir) / "remote_codex_pending_rollout.json").write_text(
            json.dumps(
                {
                    "thread-1": [
                        {
                            "timestamp": "2026-04-24T00:00:00+00:00",
                            "commandId": "command-1",
                            "text": "[TEST]hello",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
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
            "firstUserMessage": "[TEST]hello",
            "status": {"type": "inProgress"},
            "agentNickname": None,
            "agentRole": None,
        }
        backend = LocalCodexBackend(
            machine_id="homedev",
            display_name="Home Dev",
            app_server_client=FakeAppServerClient(payload={"thread": {"id": "thread-1", "turns": []}}),
            codex_home=temp_dir,
        )
        backend.get_thread_by_id = lambda thread_id: dict(thread)

        snapshot = backend.read_thread_messages("thread-1", limit=20)
        rewritten = rollout_path.read_text(encoding="utf-8")
        registry_exists = (Path(temp_dir) / "remote_codex_pending_rollout.json").exists()

    assert snapshot is not None
    assert [message["text"] for message in snapshot["messages"]] == ["[TEST]hello"]
    assert rewritten.count("[TEST]hello") == 1
    assert not registry_exists


def _sample_health(*, live_control: bool = True) -> dict[str, object]:
    return {
        "activeTransport": "standalone-app-server" if live_control else "filesystem-storage",
        "runtimeMode": "standalone-app-server" if live_control else "filesystem-readonly",
        "runtimeAvailable": live_control,
        "capabilities": {
            "threadRead": True,
            "threadLive": True,
            "liveControl": live_control,
            "approvalHandling": False,
        },
        "runtimeDescriptor": {"runtimeMode": "standalone-app-server"} if live_control else None,
        "lastRuntimeError": None,
        "lastDiagnostic": None,
    }


def test_remote_codex_device_agent_bootstrap_syncs_machine_threads_and_snapshots() -> None:
    backend = FakeBackend(
        threads=[_sample_thread()],
        snapshots={"thread-1": _sample_snapshot()},
        health=_sample_health(),
    )
    bridge = FakeBridge()
    agent = RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id="homedev",
        display_name="Home Dev",
        worker_id="homedev-agent",
    )

    worked = agent.poll_once()

    assert worked is True
    assert len(bridge.sync_calls) >= 1
    sync = bridge.sync_calls[0]
    assert sync["machine"]["machineId"] == "homedev"
    assert sync["machine"]["capabilities"]["liveControl"] is True
    assert sync["threads"][0]["id"] == "thread-1"
    assert sync["snapshots"][0]["thread"]["id"] == "thread-1"


def test_remote_codex_device_agent_limits_snapshot_messages_before_sync() -> None:
    snapshot = _sample_snapshot()
    snapshot["messages"] = [
        {
            "lineNumber": index,
            "timestamp": f"2026-04-23T00:00:{index % 60:02d}+00:00",
            "role": "user" if index % 2 else "assistant",
            "phase": None if index % 2 else "completed",
            "text": f"message {index}",
            "images": [],
        }
        for index in range(1, 251)
    ]
    snapshot["totalMessages"] = 250
    snapshot["lineCount"] = 250

    backend = FakeBackend(
        threads=[_sample_thread()],
        snapshots={"thread-1": snapshot},
        health=_sample_health(),
    )
    bridge = FakeBridge()
    agent = RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id="homedev",
        display_name="Home Dev",
        worker_id="homedev-agent",
        message_limit=60,
    )

    worked = agent.poll_once()

    assert worked is True
    assert backend.read_limits == [("thread-1", 60)]
    synced_messages = bridge.sync_calls[0]["snapshots"][0]["messages"]
    assert len(synced_messages) == 60
    assert synced_messages[0]["lineNumber"] == 191
    assert synced_messages[-1]["lineNumber"] == 250
    assert bridge.sync_calls[0]["snapshots"][0]["totalMessages"] == 250


def test_build_thread_version_changes_when_rollout_file_grows_without_thread_metadata_change() -> None:
    with TemporaryDirectory() as temp_dir:
        rollout_path = Path(temp_dir) / "rollout.jsonl"
        rollout_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"first"}}\n',
            encoding="utf-8",
        )
        thread = _sample_thread()
        thread["rolloutPath"] = str(rollout_path)

        initial_version = build_thread_version(thread)

        rollout_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"first"}}\n'
            '{"type":"event_msg","payload":{"type":"assistant_message","message":"second"}}\n',
            encoding="utf-8",
        )
        rollout_path.touch()

        updated_version = build_thread_version(thread)

        assert updated_version != initial_version


def test_remote_codex_device_agent_executes_turn_start_commands_and_reports_result() -> None:
    backend = FakeBackend(
        threads=[_sample_thread()],
        snapshots={"thread-1": _sample_snapshot()},
        health=_sample_health(),
    )
    bridge = FakeBridge(
        queued_commands=[
            {
                "commandId": "command-1",
                "type": "turn.start",
                "machineId": "homedev",
                "threadId": "thread-1",
                "taskId": "task-1",
                "prompt": "Add a real device sync loop.",
            }
        ]
    )
    agent = RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id="homedev",
        display_name="Home Dev",
        worker_id="homedev-agent",
    )

    worked = agent.poll_once()

    assert worked is True
    assert backend.start_turn_calls == [("thread-1", "Add a real device sync loop.")]
    assert backend.materialized_prompts == [("thread-1", "command-1", "Add a real device sync loop.")]
    assert backend.actions[:2] == ["materialize", "start_turn"]
    assert [item["phase"] for item in bridge.heartbeat_calls] == ["running", "executing"]
    assert bridge.evidence_calls[0]["payload"]["turnId"] == "turn-1"
    assert bridge.command_result_calls[0]["status"] == "completed"
    assert bridge.command_result_calls[0]["result"]["turnStatus"] == "inProgress"
    assert bridge.fail_calls == []
    assert len(bridge.sync_calls) >= 2


def test_remote_codex_device_agent_sync_materializes_pending_browser_prompt_before_claim() -> None:
    backend = FakeBackend(
        threads=[_sample_thread()],
        snapshots={"thread-1": _sample_snapshot()},
        health=_sample_health(),
    )
    bridge = FakeBridge(
        thread_commands_by_thread={
            "thread-1": [
                {
                    "commandId": "queued-command-1",
                    "type": "turn.start",
                    "status": "queued",
                    "prompt": "[TEST]queued prompt",
                }
            ]
        }
    )
    agent = RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id="homedev",
        display_name="Home Dev",
        worker_id="worker-1",
    )

    changed = agent.perform_sync(force=False)

    assert changed is True
    assert backend.materialized_prompts == [("thread-1", "queued-command-1", "[TEST]queued prompt")]
    assert bridge.sync_calls


def test_remote_codex_device_agent_sync_skips_rollout_materialization_when_desktop_ui_submission_is_available() -> None:
    backend = FakeBackend(
        threads=[_sample_thread()],
        snapshots={"thread-1": _sample_snapshot()},
        health=_sample_health(),
        desktop_ui_enabled_threads={"thread-1"},
    )
    bridge = FakeBridge(
        thread_commands_by_thread={
            "thread-1": [
                {
                    "commandId": "queued-command-1",
                    "type": "turn.start",
                    "status": "queued",
                    "prompt": "[TEST]queued prompt",
                }
            ]
        }
    )
    agent = RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id="homedev",
        display_name="Home Dev",
        worker_id="worker-1",
    )

    changed = agent.perform_sync(force=False)

    assert changed is True
    assert backend.materialized_prompts == []
    assert bridge.sync_calls


def test_remote_codex_device_agent_executes_turn_start_commands_via_desktop_ui_when_available() -> None:
    backend = FakeBackend(
        threads=[_sample_thread()],
        snapshots={"thread-1": _sample_snapshot()},
        health=_sample_health(),
        desktop_ui_enabled_threads={"thread-1"},
    )
    bridge = FakeBridge(
        queued_commands=[
            {
                "commandId": "command-1",
                "type": "turn.start",
                "machineId": "homedev",
                "threadId": "thread-1",
                "taskId": "task-1",
                "prompt": "Inject this through the desktop Codex composer.",
            }
        ]
    )
    agent = RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id="homedev",
        display_name="Home Dev",
        worker_id="homedev-agent",
    )

    worked = agent.poll_once()

    assert worked is True
    assert backend.desktop_ui_submit_calls == [("thread-1", "Inject this through the desktop Codex composer.")]
    assert backend.start_turn_calls == []
    assert backend.materialized_prompts == []
    assert backend.actions[:1] == ["desktop_submit"]
    assert [item["phase"] for item in bridge.heartbeat_calls] == ["running", "executing"]
    assert bridge.evidence_calls[0]["payload"]["submissionMode"] == "desktop-ipc"
    assert bridge.command_result_calls[0]["status"] == "completed"
    assert bridge.command_result_calls[0]["result"]["turnStatus"] == "queued"
    assert bridge.command_result_calls[0]["result"]["submissionMode"] == "desktop-ipc"
    assert bridge.fail_calls == []
    assert len(bridge.sync_calls) >= 2


@dataclass
class FakePromptSubmitter:
    mode: str | None
    calls: list[str] = field(default_factory=list)

    def submit_prompt(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        return self.mode


def test_windows_codex_desktop_prompt_submitter_prefers_ipc_submitter() -> None:
    ipc_submitter = FakePromptSubmitter("desktop-ipc")
    fallback_submitter = FakePromptSubmitter("desktop-ui")
    submitter = WindowsCodexDesktopPromptSubmitter(
        thread_id="thread-1",
        ipc_submitter=ipc_submitter,
        fallback_submitter=fallback_submitter,
    )

    mode = submitter.submit_prompt("[TEST]desktop submit")

    assert mode == "desktop-ipc"
    assert ipc_submitter.calls == ["[TEST]desktop submit"]
    assert fallback_submitter.calls == []


def test_windows_codex_desktop_prompt_submitter_falls_back_to_ui_submitter() -> None:
    ipc_submitter = FakePromptSubmitter(None)
    fallback_submitter = FakePromptSubmitter("desktop-ui")
    submitter = WindowsCodexDesktopPromptSubmitter(
        thread_id="thread-1",
        ipc_submitter=ipc_submitter,
        fallback_submitter=fallback_submitter,
    )

    mode = submitter.submit_prompt("[TEST]desktop submit")

    assert mode == "desktop-ui"
    assert ipc_submitter.calls == ["[TEST]desktop submit"]
    assert fallback_submitter.calls == ["[TEST]desktop submit"]


def test_remote_codex_device_agent_executes_thread_delete_commands() -> None:
    backend = FakeBackend(
        threads=[_sample_thread()],
        snapshots={"thread-1": _sample_snapshot()},
        health=_sample_health(),
    )
    bridge = FakeBridge(
        queued_commands=[
            {
                "commandId": "command-delete",
                "type": "thread.delete",
                "machineId": "homedev",
                "threadId": "thread-1",
            }
        ]
    )
    agent = RemoteCodexDeviceAgent(
        bridge=bridge,
        backend=backend,
        machine_id="homedev",
        display_name="Home Dev",
        worker_id="homedev-agent",
    )

    worked = agent.poll_once()

    assert worked is True
    assert backend.delete_calls == ["thread-1"]
    assert bridge.command_result_calls[0]["status"] == "completed"
    assert bridge.command_result_calls[0]["result"]["archived"] is True
    assert all(thread["id"] != "thread-1" for thread in bridge.sync_calls[-1]["threads"])
