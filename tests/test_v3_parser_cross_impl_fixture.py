"""Phase 6 / interop — cross-impl parser fixture verification (Python side).

Loads ``tests/fixtures/reply_prefix_cases.json`` (produced collaboratively
by personas in the parser-fixture op) and asserts every case produces
the expected ``(kind, expected_response, body)`` triple under the
Python ``BridgeAgentLoop._parse_reply_prefix`` parser.

The TypeScript counterpart at ``clients/ts-agent-loop/src/parser-fixture.test.ts``
runs the same fixture through ``parseReplyPrefix`` from agent.ts.
Both must pass for the cross-impl claim in protocol-v3-interop-findings.md
to hold. Drift between the two impls = test failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
PC_LAUNCHER_ROOT = PROJECT_ROOT / "pc_launcher"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "reply_prefix_cases.json"

if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))


def _fixture_cases():
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"fixture not generated yet: {FIXTURE_PATH}. Run the "
            f"parser-fixture op (scripts/smoke_v3_parser_fixture.sh) "
            f"first; it populates this file from persona collaboration."
        )
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        cases = json.load(f)
    if not isinstance(cases, list) or len(cases) < 25:
        pytest.fail(
            f"fixture has {len(cases) if isinstance(cases, list) else '?'} "
            f"cases; spec requires ≥25"
        )
    return cases


def _normalize_expected(case_expected: dict) -> tuple[str, dict | None, str]:
    """Translate fixture's ``expected`` dict to the parser's tuple shape."""
    kind = case_expected.get("kind") or "claim"
    ex = case_expected.get("expected_response")
    body = case_expected.get("body", "")
    if ex is not None and not ex:
        ex = None  # treat empty dict as null
    return kind, ex, body


@pytest.mark.parametrize("case_idx", range(1000))  # bound; trimmed below
def test_parser_case(case_idx, request):
    """One test per fixture case. Pytest collects up to len(cases)."""
    cases = _fixture_cases()
    if case_idx >= len(cases):
        pytest.skip("beyond fixture")
    case = cases[case_idx]

    from connectors.claude_executor.agent_loop import BridgeAgentLoop
    actual_kind, actual_ex, actual_body = BridgeAgentLoop._parse_reply_prefix(
        case["input"]
    )
    expected_kind, expected_ex, expected_body = _normalize_expected(case["expected"])

    label = case.get("label", f"#{case_idx}")
    assert actual_kind == expected_kind, (
        f"[{label}] kind mismatch on input={case['input']!r}: "
        f"got={actual_kind!r} expected={expected_kind!r}"
    )
    assert actual_body == expected_body, (
        f"[{label}] body mismatch on input={case['input']!r}: "
        f"got={actual_body!r} expected={expected_body!r}"
    )
    # expected_response comparison: both None or both same dict
    if expected_ex is None:
        assert actual_ex is None, (
            f"[{label}] expected_response should be None on input={case['input']!r}, "
            f"got {actual_ex!r}"
        )
    else:
        assert actual_ex is not None, (
            f"[{label}] expected_response should be set on input={case['input']!r}, "
            f"got None; expected {expected_ex!r}"
        )
        assert (
            actual_ex.get("from_actor_handles") == expected_ex.get("from_actor_handles")
        ), (
            f"[{label}] from_actor_handles mismatch on input={case['input']!r}: "
            f"got={actual_ex.get('from_actor_handles')!r} expected={expected_ex.get('from_actor_handles')!r}"
        )
        # `kinds` is optional; both omit-or-set must match
        if "kinds" in expected_ex:
            assert actual_ex.get("kinds") == expected_ex.get("kinds"), (
                f"[{label}] kinds mismatch on input={case['input']!r}: "
                f"got={actual_ex.get('kinds')!r} expected={expected_ex.get('kinds')!r}"
            )


def test_fixture_has_required_categories():
    """Sanity: the fixture covers every category the prompt asked
    operator to include. If a category is missing, the personas dropped
    a corner."""
    cases = _fixture_cases()
    inputs = [c["input"] for c in cases]
    # Detect each category via a structural probe
    has_terminal_no_prefix = any(not s.startswith("[") for s in inputs)
    has_kind_only_terminal = any(
        s.startswith("[") and "→" not in s and "->" not in s
        and "@" not in s.split("]", 1)[0]
        for s in inputs
    )
    has_arrow_unicode = any("→" in s.split("]", 1)[0] for s in inputs if "]" in s)
    has_arrow_ascii = any("->" in s.split("]", 1)[0] for s in inputs if "]" in s)
    has_no_arrow_at = any(
        "@" in s.split("]", 1)[0] and "→" not in s.split("]", 1)[0]
        and "->" not in s.split("]", 1)[0]
        for s in inputs if "]" in s
    )
    has_kinds_whitelist = any("kinds=" in s for s in inputs)
    has_unknown_kind = any(
        s.startswith("[") and "]" in s and
        (s[1:s.index("]")].split("@")[0].split("→")[0].split("->")[0]
         .strip().lower()) not in {
            "claim", "question", "answer", "propose", "agree", "object",
            "evidence", "block", "defer", "summarize", "react",
            "move_close", "ratify", "invite", "join",
        }
        for s in inputs
    )

    missing = []
    if not has_terminal_no_prefix: missing.append("terminal-no-prefix")
    if not has_kind_only_terminal: missing.append("kind-only-terminal")
    if not has_arrow_unicode: missing.append("unicode-arrow")
    if not has_arrow_ascii: missing.append("ascii-arrow")
    if not has_no_arrow_at: missing.append("no-arrow-@-only")
    if not has_kinds_whitelist: missing.append("kinds-whitelist")
    if not has_unknown_kind: missing.append("unknown-kind-fallback")
    assert not missing, f"fixture missing categories: {missing}"
