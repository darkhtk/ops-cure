"""F1 smoke — remote_claude routes basic shape.

Stub the service the same way test_remote_codex_behavior_scaffold does;
verify the router prefix, the 4 read endpoints, the 7 enqueue endpoints,
and the 4 agent endpoints all wire up without exception.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command(cid: str = "cmd-x", **overrides: Any) -> dict[str, Any]:
    base = {
        "id": cid, "commandId": cid, "type": "run.start", "status": "queued",
        "machineId": "machine-a", "sessionId": "", "runId": None,
        "prompt": None, "result": None, "error": None,
        "createdAt": _now(), "updatedAt": _now(),
    }
    base.update(overrides)
    return base


class StubService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # subscribe_session / publish_event need a state_service handle —
        # we mock just enough for SSE to be skipped (smoke doesn't subscribe).
        self.state_service = SimpleNamespace(
            subscribe_session=lambda *a, **kw: None,
        )

    def _record(self, _call_name: str, **kwargs: Any) -> None:
        self.calls.append((_call_name, kwargs))

    def list_machines(self) -> dict[str, Any]:
        self._record("list_machines")
        return {"machines": []}

    def list_sessions(self, machine_id: str, *, limit: int = 200) -> dict[str, Any]:
        self._record("list_sessions", machine_id=machine_id, limit=limit)
        return {"sessions": []}

    def get_session(self, machine_id: str, session_id: str) -> dict[str, Any]:
        self._record("get_session", machine_id=machine_id, session_id=session_id)
        if session_id == "missing":
            raise ValueError("session_not_found")
        return {"session": {"sessionId": session_id, "machineId": machine_id}}

    def enqueue_run_start(self, **kwargs: Any) -> dict[str, Any]:
        self._record("enqueue_run_start", **kwargs)
        return {"ok": True, "command": _command("cmd-run-start", type="run.start")}

    def enqueue_run_input(self, **kwargs: Any) -> dict[str, Any]:
        self._record("enqueue_run_input", **kwargs)
        return {"ok": True, "command": _command("cmd-run-input", type="run.input")}

    def enqueue_run_interrupt(self, **kwargs: Any) -> dict[str, Any]:
        self._record("enqueue_run_interrupt", **kwargs)
        return {"ok": True, "command": _command("cmd-int", type="run.interrupt")}

    def enqueue_session_delete(self, **kwargs: Any) -> dict[str, Any]:
        self._record("enqueue_session_delete", **kwargs)
        return {"ok": True, "command": _command("cmd-del", type="session.delete")}

    def enqueue_fs_list(self, **kwargs: Any) -> dict[str, Any]:
        self._record("enqueue_fs_list", **kwargs)
        return {"ok": True, "command": _command("cmd-fs-list", type="fs.list")}

    def enqueue_fs_mkdir(self, **kwargs: Any) -> dict[str, Any]:
        self._record("enqueue_fs_mkdir", **kwargs)
        return {"ok": True, "command": _command("cmd-fs-mkdir", type="fs.mkdir")}

    def enqueue_approval_respond(self, **kwargs: Any) -> dict[str, Any]:
        self._record("enqueue_approval_respond", **kwargs)
        return {"ok": True, "command": _command("cmd-approval", type="approval.respond")}

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        self._record("get_command", command_id=command_id)
        if command_id == "missing":
            return None
        return _command(command_id, status="completed")

    def agent_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._record("agent_sync", payload=payload)
        return {"ok": True, "machine": payload.get("machine") or {}, "sessionCount": 0}

    def agent_claim_command(self, machine_id: str, *, worker_id: str) -> dict[str, Any]:
        self._record("agent_claim_command", machine_id=machine_id, worker_id=worker_id)
        return {"command": None}

    def agent_report_command_result(self, command_id: str, **kwargs: Any) -> dict[str, Any]:
        self._record("agent_report_command_result", command_id=command_id, **kwargs)
        return {"command": _command(command_id, status="completed")}

    def agent_publish_event(self, **kwargs: Any) -> None:
        self._record("agent_publish_event", **kwargs)


def _client() -> tuple[TestClient, StubService]:
    from app.behaviors.remote_claude.api import router

    service = StubService()
    app = FastAPI()
    app.state.services = SimpleNamespace(remote_claude_service=service)
    app.include_router(router)
    return TestClient(app), service


def _hdr() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


# ---- read ------------------------------------------------------------------

def test_router_prefix_is_remote_claude(app_env) -> None:
    from app.behaviors.remote_claude.api import router
    assert router.prefix == "/api/remote-claude"


def test_list_machines_smoke(app_env) -> None:
    client, service = _client()
    r = client.get("/api/remote-claude/machines", headers=_hdr())
    assert r.status_code == 200, r.text
    assert r.json() == {"machines": []}
    assert service.calls[-1][0] == "list_machines"


def test_list_sessions_smoke(app_env) -> None:
    client, service = _client()
    r = client.get("/api/remote-claude/machines/machine-a/sessions?limit=50", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"sessions": []}
    assert service.calls[-1][1]["machine_id"] == "machine-a"
    assert service.calls[-1][1]["limit"] == 50


def test_get_session_404_when_missing(app_env) -> None:
    client, _ = _client()
    r = client.get("/api/remote-claude/machines/machine-a/sessions/missing", headers=_hdr())
    assert r.status_code == 404


def test_get_session_200(app_env) -> None:
    client, _ = _client()
    r = client.get("/api/remote-claude/machines/machine-a/sessions/abc", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["session"]["sessionId"] == "abc"


# ---- enqueue ---------------------------------------------------------------

def test_run_start_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/machines/machine-a/sessions",
        headers=_hdr(),
        json={"cwd": "C:/proj", "prompt": "hi", "model": "claude-opus", "permissionMode": "default"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["command"]["type"] == "run.start"
    last = service.calls[-1]
    assert last[0] == "enqueue_run_start"
    assert last[1]["cwd"] == "C:/proj"
    assert last[1]["prompt"] == "hi"
    assert last[1]["model"] == "claude-opus"
    assert last[1]["permission_mode"] == "default"


def test_run_start_400_when_cwd_missing(app_env) -> None:
    client, _ = _client()
    r = client.post(
        "/api/remote-claude/machines/machine-a/sessions",
        headers=_hdr(),
        json={"prompt": "hi"},
    )
    assert r.status_code == 400


def test_run_input_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/machines/machine-a/sessions/sess-1/input",
        headers=_hdr(),
        json={"text": "follow up", "attachments": [{"name": "x.png", "mimeType": "image/png"}]},
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "enqueue_run_input"
    assert last[1]["text"] == "follow up"
    assert last[1]["attachments"][0]["name"] == "x.png"


def test_run_input_400_when_empty(app_env) -> None:
    client, _ = _client()
    r = client.post(
        "/api/remote-claude/machines/machine-a/sessions/sess-1/input",
        headers=_hdr(),
        json={"text": "", "attachments": []},
    )
    assert r.status_code == 400


def test_run_interrupt_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/machines/machine-a/sessions/sess-1/interrupt",
        headers=_hdr(),
        json={"runId": "run-1"},
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "enqueue_run_interrupt"
    assert last[1]["run_id"] == "run-1"


def test_session_delete_routes(app_env) -> None:
    client, service = _client()
    r = client.delete(
        "/api/remote-claude/machines/machine-a/sessions/sess-1",
        headers=_hdr(),
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "enqueue_session_delete"
    assert last[1]["session_id"] == "sess-1"


def test_fs_list_routes(app_env) -> None:
    client, service = _client()
    r = client.get(
        "/api/remote-claude/machines/machine-a/fs/list?path=C%3A%2Fproj",
        headers=_hdr(),
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "enqueue_fs_list"
    assert last[1]["path"] == "C:/proj"


def test_fs_mkdir_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/machines/machine-a/fs/mkdir",
        headers=_hdr(),
        json={"parent": "C:/proj", "name": "newdir"},
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "enqueue_fs_mkdir"
    assert last[1]["parent"] == "C:/proj"
    assert last[1]["name"] == "newdir"


def test_approval_respond_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/machines/machine-a/sessions/sess-1/approval",
        headers=_hdr(),
        json={"approvalId": "ap-1", "decision": "allow", "reason": "ok"},
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "enqueue_approval_respond"
    assert last[1]["decision"] == "allow"


def test_get_command_404(app_env) -> None:
    client, _ = _client()
    r = client.get("/api/remote-claude/commands/missing", headers=_hdr())
    assert r.status_code == 404


def test_get_command_200(app_env) -> None:
    client, _ = _client()
    r = client.get("/api/remote-claude/commands/cmd-x", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["command"]["status"] == "completed"


# ---- agent endpoints --------------------------------------------------------

def test_agent_sync_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/agent/sync",
        headers=_hdr(),
        json={"machine": {"machineId": "homedev", "displayName": "homedev"}, "sessions": []},
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "agent_sync"


def test_agent_claim_command_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/agent/commands/claim",
        headers=_hdr(),
        json={"machineId": "homedev", "workerId": "worker-1"},
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "agent_claim_command"


def test_agent_claim_400_when_missing(app_env) -> None:
    client, _ = _client()
    r = client.post(
        "/api/remote-claude/agent/commands/claim",
        headers=_hdr(),
        json={},
    )
    assert r.status_code == 400


def test_agent_report_result_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/agent/commands/cmd-x/result",
        headers=_hdr(),
        json={"workerId": "w", "status": "completed", "result": {}},
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "agent_report_command_result"


def test_agent_events_routes(app_env) -> None:
    client, service = _client()
    r = client.post(
        "/api/remote-claude/agent/events",
        headers=_hdr(),
        json={
            "machineId": "homedev",
            "sessionId": "sess-1",
            "event": {"kind": "claude.event", "event": {"type": "assistant"}},
        },
    )
    assert r.status_code == 200
    last = service.calls[-1]
    assert last[0] == "agent_publish_event"


def test_agent_events_400_bad_payload(app_env) -> None:
    client, _ = _client()
    r = client.post(
        "/api/remote-claude/agent/events",
        headers=_hdr(),
        json={"machineId": "homedev"},
    )
    assert r.status_code == 400
