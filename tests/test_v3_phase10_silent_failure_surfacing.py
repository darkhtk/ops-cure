"""Phase 10 — silent-failure surfaces in agent_loop.

Each test exercises a state-recording slot that the prompt builder
reads on the next turn:

  P10.1 + P10.5  pre-flight reject when ``[KIND]`` is mid-body
  P10.2          ``ARTIFACT:`` header path unstat-able → next-prompt warn
  P10.3          5xx capture (transient flag) alongside 4xx
  P10.6          consecutive same-kind 4xx escalates next-prompt warning

Pre-Phase-10 each of these failed silently — agent_loop logged to
stderr but the LLM had no awareness on its next turn.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

PC_LAUNCHER_ROOT = Path(__file__).parent.parent / "pc_launcher"
if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))


class _StubLoop:
    """Bare-bones loop stand-in carrying the slots the relevant
    methods touch. Avoid SSE / network."""

    _ARTIFACT_HEADER_PREFIX = "ARTIFACT:"

    def __init__(self, *, cwd=None) -> None:
        self._actor_handle = "@operator"
        self._system_prompt = ""
        self._history_limit = 0
        self._cwd = str(cwd) if cwd else None
        self._log_lines: list[str] = []
        self._last_post_rejection: dict = {}
        self._last_run_failure: dict = {}
        self._last_artifact_failure: dict = {}
        self._consecutive_kind_failures: dict = {}

    def _log(self, msg: str): self._log_lines.append(msg)
    def _fetch_op_history(self, _op_id): return []


def _bind(stub: _StubLoop):
    from connectors.claude_executor.agent_loop import BridgeAgentLoop
    stub._build_prompt = BridgeAgentLoop._build_prompt.__get__(stub)
    stub._maybe_extract_artifact = BridgeAgentLoop._maybe_extract_artifact.__get__(stub)
    stub._maybe_extract_artifacts = BridgeAgentLoop._maybe_extract_artifacts.__get__(stub)
    return stub


# ---------------------------------------------------------------------
# P10.2 — artifact-extract failure → next prompt
# ---------------------------------------------------------------------

def test_p10_2_missing_path_records_artifact_failure(tmp_path):
    stub = _bind(_StubLoop(cwd=tmp_path))
    body = "ARTIFACT: path=does_not_exist.txt kind=code\nbody"
    art, rest = stub._maybe_extract_artifact(body)
    assert art is None
    assert hasattr(stub, "_pending_artifact_failure")
    assert "does_not_exist.txt" in stub._pending_artifact_failure["path"]
    assert "unreadable" in stub._pending_artifact_failure["reason"]


def test_p10_2_no_path_field_records_artifact_failure(tmp_path):
    stub = _bind(_StubLoop(cwd=tmp_path))
    body = "ARTIFACT: kind=code label=oops\nbody"
    art, _ = stub._maybe_extract_artifact(body)
    assert art is None
    assert "missing" in stub._pending_artifact_failure["path"]


def test_p10_2_failure_surfaces_in_next_prompt(tmp_path):
    stub = _bind(_StubLoop(cwd=tmp_path))
    stub._last_artifact_failure["op-1"] = {
        "path": "/missing/file.exe",
        "reason": "unreadable: not a file",
        "ts": "2026-05-04T00:00:00Z",
    }
    out = stub._build_prompt({
        "operation_id": "op-1",
        "kind": "chat.speech.question",
        "payload": {"text": "next"},
    })
    assert "COULD NOT BE ATTACHED" in out
    assert "/missing/file.exe" in out


# ---------------------------------------------------------------------
# P10.3 — 5xx transient flag in rejection surface
# ---------------------------------------------------------------------

def test_p10_3_transient_5xx_uses_distinct_guidance():
    stub = _bind(_StubLoop())
    stub._last_post_rejection["op-x"] = {
        "detail": "Internal Server Error",
        "rejected_kind": "evidence",
        "http_status": "503",
        "transient": "true",
    }
    out = stub._build_prompt({
        "operation_id": "op-x",
        "kind": "chat.speech.claim",
        "payload": {"text": "n"},
    })
    assert "transient" in out
    assert "5xx" in out


def test_p10_3_4xx_not_transient_uses_strict_guidance():
    stub = _bind(_StubLoop())
    stub._last_post_rejection["op-x"] = {
        "detail": "policy.reply_kind_rejected: ...",
        "rejected_kind": "evidence",
        "http_status": "400",
        "transient": "false",
    }
    out = stub._build_prompt({
        "operation_id": "op-x",
        "kind": "chat.speech.claim",
        "payload": {"text": "n"},
    })
    assert "transient" not in out.split("⚠️")[1] if "⚠️" in out else True
    assert "Adjust your reply" in out or "carve-out" in out.lower()


# ---------------------------------------------------------------------
# P10.6 — consecutive same-kind escalates
# ---------------------------------------------------------------------

def test_p10_6_first_failure_does_not_escalate():
    stub = _bind(_StubLoop())
    stub._last_post_rejection["op-y"] = {
        "detail": "policy.reply_kind_rejected: only [agree, object]",
        "rejected_kind": "evidence",
        "http_status": "400",
        "transient": "false",
    }
    stub._consecutive_kind_failures["op-y"] = {"evidence": 1}
    out = stub._build_prompt({
        "operation_id": "op-y",
        "kind": "chat.speech.claim",
        "payload": {"text": "n"},
    })
    assert "🚨" not in out


def test_p10_6_second_consecutive_failure_escalates():
    stub = _bind(_StubLoop())
    stub._last_post_rejection["op-y"] = {
        "detail": "policy.reply_kind_rejected: only [agree, object]",
        "rejected_kind": "evidence",
        "http_status": "400",
        "transient": "false",
    }
    stub._consecutive_kind_failures["op-y"] = {"evidence": 2}
    out = stub._build_prompt({
        "operation_id": "op-y",
        "kind": "chat.speech.claim",
        "payload": {"text": "n"},
    })
    assert "🚨" in out
    assert "2 turns" in out
    assert "Stop trying" in out


# ---------------------------------------------------------------------
# P10.1 + P10.5 — mid-body prefix rejected pre-flight
# ---------------------------------------------------------------------

def test_p10_5_pure_claim_passes_through():
    """Plain claim with no mid-body prefix is fine."""
    from connectors.claude_executor.agent_loop import BridgeAgentLoop
    text = "Just a normal claim with @bob mentioned in prose."
    kind, _, _ = BridgeAgentLoop._parse_reply_prefix(text)
    # If parser falls back to claim AND the body has no mid-body prefix,
    # post should NOT be rejected. We test the parser directly here;
    # the pre-flight check sits in _post_claim.
    assert kind == "claim"


def test_p10_5_mid_body_prefix_detection_logic():
    """Probe the mid-body detection logic directly: given a body that
    parses as plain CLAIM (because it doesn't start with `[`), the
    pre-flight check scans head-lines for any line that does start
    with `[` and would parse to a recognized kind. That signals the
    LLM intended structured form but missed position-0."""
    from connectors.claude_executor.agent_loop import BridgeAgentLoop
    body = "Phase A is green.\n[EVIDENCE→@reviewer]\nARTIFACT: path=x"
    head_lines = body.splitlines()[:10]
    # The first line is plain prose — parser falls to claim.
    kind1, _, _ = BridgeAgentLoop._parse_reply_prefix(body)
    assert kind1 == "claim"
    # The second line, parsed as if it were the body, would yield 'evidence'.
    second_line = head_lines[1].lstrip()
    probe_kind, _, _ = BridgeAgentLoop._parse_reply_prefix(second_line)
    assert probe_kind == "evidence", (
        f"second-line probe should detect evidence, got {probe_kind}"
    )
