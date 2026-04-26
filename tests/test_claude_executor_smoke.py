"""F2 smoke — claude_executor agent dispatch + fs helpers + session_sync.

We don't smoke-spawn an actual claude CLI here (that needs a configured
PC). The smoke covers:

  - fs.list / fs.mkdir helpers on a real tempdir
  - session_sync.scan_sessions parses a fake jsonl
  - Agent dispatch: each command type routes to the right handler with
    a stub bridge (no real HTTP)
  - parse_payload decodes JSON-encoded prompt
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest


# Ensure pc_launcher is importable.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_launcher.connectors.claude_executor import agent as agent_mod
from pc_launcher.connectors.claude_executor import session_sync


# -------- fs helpers ---------------------------------------------------

def test_fs_list_real_directory() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "alpha").mkdir()
        Path(root, "beta").mkdir()
        Path(root, ".hidden").mkdir()
        (Path(root) / "file.txt").write_text("x")
        result = agent_mod._fs_list_impl(root)
        names = [e["name"] for e in result["entries"]]
        assert names == ["alpha", "beta"]
        assert all(e["isDir"] for e in result["entries"])
        assert result["path"] == str(Path(root).resolve())


def test_fs_list_missing_path_raises() -> None:
    with pytest.raises(FileNotFoundError):
        agent_mod._fs_list_impl("/definitely/not/here/ever")


def test_fs_mkdir_creates_then_rejects_duplicate() -> None:
    with tempfile.TemporaryDirectory() as root:
        result = agent_mod._fs_mkdir_impl(root, "newdir")
        assert Path(result["path"]).is_dir()
        with pytest.raises(FileExistsError):
            agent_mod._fs_mkdir_impl(root, "newdir")


def test_fs_mkdir_rejects_separator_and_dotdot() -> None:
    with tempfile.TemporaryDirectory() as root:
        for bad in ["a/b", "a\\b", "..", ".", "with<chev"]:
            with pytest.raises((ValueError,)):
                agent_mod._fs_mkdir_impl(root, bad)


def test_fs_mkdir_rejects_missing_parent() -> None:
    with pytest.raises(FileNotFoundError):
        agent_mod._fs_mkdir_impl("/no/such/parent", "x")


# -------- session_sync ---------------------------------------------------

def test_scan_sessions_parses_first_user_message_and_cwd() -> None:
    with tempfile.TemporaryDirectory() as root:
        proj = Path(root) / "C--Users-darkh-Projects-foo"
        proj.mkdir()
        records = [
            {"type": "queue-operation"},
            {"type": "user", "message": {"role": "user", "content": "hello"},
             "cwd": "C:\\Users\\darkh\\Projects\\foo"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
        ]
        sid = "11111111-1111-1111-1111-111111111111"
        (proj / f"{sid}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        sessions = session_sync.scan_sessions(Path(root))
        assert len(sessions) == 1
        s = sessions[0]
        assert s["sessionId"] == sid
        assert s["title"] == "hello"
        assert s["cwd"] == "C:\\Users\\darkh\\Projects\\foo"
        assert s["eventCount"] == 3


def test_scan_sessions_falls_back_to_decoded_dirname_when_cwd_missing() -> None:
    with tempfile.TemporaryDirectory() as root:
        proj = Path(root) / "C--Users-darkh-Projects-bar"
        proj.mkdir()
        sid = "22222222-2222-2222-2222-222222222222"
        (proj / f"{sid}.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "ok"}}) + "\n",
            encoding="utf-8",
        )
        sessions = session_sync.scan_sessions(Path(root))
        assert sessions[0]["cwd"].lower().replace("/", "\\").startswith("c:\\users\\darkh\\projects\\bar")


# -------- agent dispatch -------------------------------------------------

class StubBridge:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    def report_result(self, command_id: str, *, status: str, result=None, error=None) -> None:
        self.results.append((command_id, status, result, error))


def _make_agent(bridge: StubBridge) -> agent_mod.ClaudeExecutorAgent:
    a = agent_mod.ClaudeExecutorAgent(
        bridge=bridge, machine_id="m", display_name="m",
        sync_interval_seconds=999, poll_interval_seconds=999,
    )
    return a


def test_dispatch_fs_list_completes_with_entries() -> None:
    bridge = StubBridge()
    a = _make_agent(bridge)
    with tempfile.TemporaryDirectory() as root:
        Path(root, "alpha").mkdir()
        a._dispatch({
            "commandId": "cmd-1", "type": "fs.list", "machineId": "m", "sessionId": "",
            "prompt": json.dumps({"path": root}),
        })
    assert len(bridge.results) == 1
    cid, status, result, error = bridge.results[0]
    assert cid == "cmd-1"
    assert status == "completed"
    assert result["entries"][0]["name"] == "alpha"


def test_dispatch_fs_mkdir_completes() -> None:
    bridge = StubBridge()
    a = _make_agent(bridge)
    with tempfile.TemporaryDirectory() as root:
        a._dispatch({
            "commandId": "cmd-2", "type": "fs.mkdir", "machineId": "m", "sessionId": "",
            "prompt": json.dumps({"parent": root, "name": "fresh"}),
        })
        assert Path(root, "fresh").is_dir()
    cid, status, result, _ = bridge.results[0]
    assert status == "completed"
    assert result["name"] == "fresh"


def test_dispatch_session_delete_invalid_id_fails() -> None:
    bridge = StubBridge()
    a = _make_agent(bridge)
    a._dispatch({
        "commandId": "cmd-3", "type": "session.delete",
        "machineId": "m", "sessionId": "not-a-uuid",
    })
    cid, status, _, error = bridge.results[0]
    assert status == "failed"
    assert "invalid" in (error or {}).get("message", "")


def test_dispatch_unknown_type_fails() -> None:
    bridge = StubBridge()
    a = _make_agent(bridge)
    a._dispatch({"commandId": "cmd-4", "type": "weird.thing", "machineId": "m", "sessionId": ""})
    cid, status, _, error = bridge.results[0]
    assert status == "failed"


def test_dispatch_approval_respond_acks() -> None:
    bridge = StubBridge()
    a = _make_agent(bridge)
    a._dispatch({"commandId": "cmd-5", "type": "approval.respond", "machineId": "m", "sessionId": "s",
                 "prompt": json.dumps({"approvalId": "ap1", "decision": "allow"})})
    _, status, _, _ = bridge.results[0]
    assert status == "completed"


def test_parse_payload_handles_missing_or_invalid_json() -> None:
    bridge = StubBridge()
    a = _make_agent(bridge)
    assert a._parse_payload({"prompt": json.dumps({"a": 1})}) == {"a": 1}
    assert a._parse_payload({}) == {}
    assert a._parse_payload({"prompt": "not json"}) == {}


def test_uuid_validator() -> None:
    assert agent_mod._is_session_id("11111111-1111-1111-1111-111111111111")
    assert not agent_mod._is_session_id("abc")
    assert not agent_mod._is_session_id("")
