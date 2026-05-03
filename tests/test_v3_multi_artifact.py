"""P9.3 / D11 — multiple artifacts per evidence event.

Pre-fix: ``payload.artifact`` was singular. An evidence event
attempting to attach 3 artifacts (exe + log + source) had to be
split into 3 separate events, fragmenting the audit trail. The
agent_loop's ARTIFACT header parser only consumed the first line
even when the agent's reply listed multiple.

Post-fix:
  - Bridge accepts ``payload.artifacts: list`` (plural) alongside
    the legacy ``payload.artifact: dict`` (singular). Both may be
    present — the bridge attaches every normalized entry in
    document order.
  - Agent_loop consumes consecutive ``ARTIFACT:`` header lines
    from the start of the evidence body and emits the plural form
    when more than one is present.
  - Singular form preserved on the wire when len==1 so existing
    T1.2 consumers don't change.
"""
from __future__ import annotations

import sys
import uuid

from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv(
        "BRIDGE_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'b.db').as_posix()}",
    )
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.models import ChatThreadModel
    from app.main import app
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread, suffix="d11"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}
_SHA1 = "0123456789abcdef" * 4
_SHA2 = "fedcba9876543210" * 4
_SHA3 = "1111222233334444" * 4


def _open(client, *, discord):
    r = client.post("/v2/operations", json={
        "space_id": discord, "kind": "inquiry",
        "title": "multi-art probe", "opener_actor_handle": "@alice",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_plural_artifacts_attaches_each_in_order(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="plural")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)

        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "Phase A: build + log + source attached",
                    "artifacts": [
                        {"kind": "file", "uri": "file:///exe", "sha256": _SHA1,
                         "mime": "application/x-msdownload", "size_bytes": 91000000,
                         "label": "build"},
                        {"kind": "log", "uri": "file:///build.log", "sha256": _SHA2,
                         "mime": "text/plain", "size_bytes": 2048,
                         "label": "buildlog"},
                        {"kind": "code", "uri": "file:///BuildScript.cs", "sha256": _SHA3,
                         "mime": "text/x-csharp", "size_bytes": 1500,
                         "label": "BuildScript"},
                    ],
                },
            },
        )
        assert r.status_code == 201, r.text

        r = client.get(f"/v2/operations/{op}/artifacts")
        body = r.json()
        assert len(body["artifacts"]) == 3
        kinds = [a["kind"] for a in body["artifacts"]]
        assert kinds == ["file", "log", "code"]
        sha = {a["sha256"] for a in body["artifacts"]}
        assert sha == {_SHA1, _SHA2, _SHA3}


def test_singular_and_plural_both_present(tmp_path, monkeypatch):
    """Caller may set both ``artifact`` and ``artifacts`` — bridge
    attaches both, with the singular first (it's a single dict, so
    it goes ahead of the list per spec)."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="both")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "ack",
                    "artifact": {
                        "kind": "code", "uri": "file:///single.py",
                        "sha256": _SHA1, "mime": "text/x-python",
                        "size_bytes": 100,
                    },
                    "artifacts": [
                        {"kind": "log", "uri": "file:///x.log", "sha256": _SHA2,
                         "mime": "text/plain", "size_bytes": 50},
                    ],
                },
            },
        )
        assert r.status_code == 201
        r = client.get(f"/v2/operations/{op}/artifacts")
        body = r.json()
        assert len(body["artifacts"]) == 2


def test_plural_with_one_invalid_member_rejects_400(tmp_path, monkeypatch):
    """Any malformed artifact in the plural list should reject the
    whole event with HTTP 400 — silent partial-attach hides bugs."""
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="malformed-list")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {
                    "text": "trying",
                    "artifacts": [
                        {"kind": "code", "uri": "file:///ok.py", "sha256": _SHA1,
                         "mime": "text/plain", "size_bytes": 1},
                        {"kind": "code", "sha256": "bad-not-64-hex"},
                    ],
                },
            },
        )
        assert r.status_code == 400
        assert "artifacts[1]" in r.json()["detail"]


def test_empty_plural_list_is_noop(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="empty-list")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        op = _open(client, discord=discord)
        r = client.post(
            f"/v2/operations/{op}/events",
            json={
                "actor_handle": "@operator",
                "kind": "speech.evidence",
                "payload": {"text": "nothing", "artifacts": []},
            },
        )
        assert r.status_code == 201
        r = client.get(f"/v2/operations/{op}/artifacts")
        assert r.json()["artifacts"] == []


def test_agent_loop_extracts_consecutive_artifact_headers(tmp_path):
    """Stub-test the agent_loop side: multiple ARTIFACT lines at the
    start of the body all get extracted."""
    PC_LAUNCHER_ROOT = (
        __import__("pathlib").Path(__file__).parent.parent / "pc_launcher"
    )
    if str(PC_LAUNCHER_ROOT) not in sys.path:
        sys.path.insert(0, str(PC_LAUNCHER_ROOT))

    f1 = tmp_path / "exe.txt"
    f1.write_text("a", encoding="utf-8")
    f2 = tmp_path / "log.txt"
    f2.write_text("bb", encoding="utf-8")
    f3 = tmp_path / "src.cs"
    f3.write_text("ccc", encoding="utf-8")

    from connectors.claude_executor.agent_loop import BridgeAgentLoop

    class _Stub:
        _cwd = str(tmp_path)
        _ARTIFACT_HEADER_PREFIX = BridgeAgentLoop._ARTIFACT_HEADER_PREFIX
        _log_lines: list[str] = []
        def _log(self, m: str): self._log_lines.append(m)

    s = _Stub()
    s._maybe_extract_artifact = (
        BridgeAgentLoop._maybe_extract_artifact.__get__(s)
    )
    s._maybe_extract_artifacts = (
        BridgeAgentLoop._maybe_extract_artifacts.__get__(s)
    )

    body = (
        "ARTIFACT: path=exe.txt kind=file label=exe\n"
        "ARTIFACT: path=log.txt kind=log label=log\n"
        "ARTIFACT: path=src.cs kind=code label=src\n"
        "Phase A green: 3 deliverables ready."
    )
    artifacts, rest = s._maybe_extract_artifacts(body)
    assert len(artifacts) == 3
    labels = [a["label"] for a in artifacts]
    assert labels == ["exe", "log", "src"]
    assert artifacts[0]["size_bytes"] == 1
    assert artifacts[1]["size_bytes"] == 2
    assert artifacts[2]["size_bytes"] == 3
    assert rest == "Phase A green: 3 deliverables ready."


def test_agent_loop_stops_on_non_artifact_line(tmp_path):
    """First non-ARTIFACT line ends the header block."""
    PC_LAUNCHER_ROOT = (
        __import__("pathlib").Path(__file__).parent.parent / "pc_launcher"
    )
    if str(PC_LAUNCHER_ROOT) not in sys.path:
        sys.path.insert(0, str(PC_LAUNCHER_ROOT))

    (tmp_path / "a.txt").write_text("aa", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bb", encoding="utf-8")

    from connectors.claude_executor.agent_loop import BridgeAgentLoop

    class _Stub:
        _cwd = str(tmp_path)
        _ARTIFACT_HEADER_PREFIX = BridgeAgentLoop._ARTIFACT_HEADER_PREFIX
        _log_lines: list[str] = []
        def _log(self, m: str): self._log_lines.append(m)

    s = _Stub()
    s._maybe_extract_artifact = BridgeAgentLoop._maybe_extract_artifact.__get__(s)
    s._maybe_extract_artifacts = BridgeAgentLoop._maybe_extract_artifacts.__get__(s)
    body = (
        "ARTIFACT: path=a.txt kind=code\n"
        "Phase A green description.\n"
        "ARTIFACT: path=b.txt kind=log\n"
        "this should not be picked up.\n"
    )
    artifacts, rest = s._maybe_extract_artifacts(body)
    assert len(artifacts) == 1
    assert "a.txt" in artifacts[0]["uri"]
    assert rest.startswith("Phase A green")
    assert "ARTIFACT: path=b.txt" in rest
