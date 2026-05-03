"""T1.2 — agent_loop's ARTIFACT header extraction.

When an evidence-kind reply starts with an ``ARTIFACT: path=...``
header line, ``BridgeAgentLoop._maybe_extract_artifact`` resolves
the path under ``self._cwd``, computes sha256 + size, guesses
mime, and returns a structured dict ready for the bridge's
``payload.artifact`` slot.

This test exercises the parser without touching the network or
running claude — we instantiate a stub loop, populate ``_cwd``
with a tmp dir, write a file, and assert the returned dict.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

PC_LAUNCHER_ROOT = Path(__file__).parent.parent / "pc_launcher"
if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))


class _StubAgentLoop:
    """Minimal stand-in carrying just the methods + state the
    extractor reaches for. Avoids constructing a full
    ``BridgeAgentLoop`` (which expects an SSE stream + a real
    bridge URL)."""

    def __init__(self, cwd: Path | None) -> None:
        self._cwd = str(cwd) if cwd else None
        self._log_lines: list[str] = []

    def _log(self, msg: str) -> None:
        self._log_lines.append(msg)


def _bind(stub: _StubAgentLoop):
    """Attach _maybe_extract_artifact + _ARTIFACT_HEADER_PREFIX
    from BridgeAgentLoop onto the stub so the method runs against
    our minimal state."""
    from connectors.claude_executor.agent_loop import BridgeAgentLoop
    stub._ARTIFACT_HEADER_PREFIX = BridgeAgentLoop._ARTIFACT_HEADER_PREFIX
    stub._maybe_extract_artifact = (
        BridgeAgentLoop._maybe_extract_artifact.__get__(stub)
    )
    return stub


def test_extracts_artifact_with_full_metadata(tmp_path):
    f = tmp_path / "dodge.html"
    f.write_text(
        "<!doctype html><body><script>game</script></body>",
        encoding="utf-8",
    )
    expected_sha = hashlib.sha256(f.read_bytes()).hexdigest()
    expected_size = f.stat().st_size

    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "ARTIFACT: path=dodge.html kind=code label=dodge-v1\nWrote dodge.html"
    artifact, rest = stub._maybe_extract_artifact(body)
    assert artifact is not None
    assert artifact["kind"] == "code"
    assert artifact["sha256"] == expected_sha
    assert artifact["size_bytes"] == expected_size
    assert artifact["mime"].startswith("text/html")
    assert artifact["uri"].startswith("file:")
    assert "dodge.html" in artifact["uri"]
    assert artifact["label"] == "dodge-v1"
    assert rest == "Wrote dodge.html"


def test_no_artifact_header_returns_unchanged(tmp_path):
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "Just a normal evidence claim with no artifact"
    artifact, rest = stub._maybe_extract_artifact(body)
    assert artifact is None
    assert rest == body


def test_artifact_header_missing_path_logs_and_skips(tmp_path):
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "ARTIFACT: kind=screenshot\nbody text"
    artifact, rest = stub._maybe_extract_artifact(body)
    assert artifact is None
    # The header line was stripped (a malformed marker is still a
    # marker — body should be just the prose).
    assert rest == "body text"
    assert any("missing path=" in line for line in stub._log_lines)


def test_artifact_header_missing_file_logs_and_skips(tmp_path):
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "ARTIFACT: path=does_not_exist.txt\nfollowup body"
    artifact, rest = stub._maybe_extract_artifact(body)
    assert artifact is None
    assert rest == "followup body"
    assert any("unreadable" in line for line in stub._log_lines)


def test_artifact_header_default_kind_is_file(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("hello", encoding="utf-8")
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "ARTIFACT: path=log.txt\nrest"
    artifact, rest = stub._maybe_extract_artifact(body)
    assert artifact is not None
    assert artifact["kind"] == "file"  # default when not specified
    assert "label" not in artifact     # optional, omitted by default


def test_extracts_unknown_extension_as_octet_stream(tmp_path):
    f = tmp_path / "data.weird"
    f.write_bytes(b"\x00\x01\x02")
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "ARTIFACT: path=data.weird\n"
    artifact, _ = stub._maybe_extract_artifact(body)
    assert artifact is not None
    assert artifact["mime"] == "application/octet-stream"


def test_path_with_subdirectory(tmp_path):
    sub = tmp_path / "out" / "build"
    sub.mkdir(parents=True)
    f = sub / "result.json"
    f.write_text('{"ok": true}', encoding="utf-8")
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "ARTIFACT: path=out/build/result.json kind=evidence\n"
    artifact, _ = stub._maybe_extract_artifact(body)
    assert artifact is not None
    assert artifact["mime"].startswith("application/json")
    assert "out/build/result.json" in artifact["uri"].replace("\\", "/")


def test_no_newline_after_header_consumes_body(tmp_path):
    """Edge: ``ARTIFACT: path=x.txt`` (no trailing newline) means the
    whole input was the header — body becomes empty."""
    f = tmp_path / "x.txt"
    f.write_text("a", encoding="utf-8")
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    artifact, rest = stub._maybe_extract_artifact("ARTIFACT: path=x.txt")
    assert artifact is not None
    assert rest == ""


def test_does_not_misfire_on_artifact_in_middle_of_body(tmp_path):
    """Marker is recognized only as a *first-line* prefix. Embedded
    ARTIFACT mid-text is not a marker — it's prose."""
    stub = _bind(_StubAgentLoop(cwd=tmp_path))
    body = "Here is the result.\nARTIFACT: path=foo.txt\nmore text"
    artifact, rest = stub._maybe_extract_artifact(body)
    assert artifact is None
    assert rest == body  # unchanged
