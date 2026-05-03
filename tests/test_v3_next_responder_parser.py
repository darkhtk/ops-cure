"""Phase 6 — agent reply prefix parser.

Tests the ``BridgeAgentLoop._parse_reply_prefix`` static parser
without requiring a running bridge. Pin down the grammar:

  [KIND] body                        → (kind, None, body)
  [KIND@a,@b] body                   → (kind, {from:[@a,@b]}, body)
  [KIND→@a,@b] body                  → same (unicode arrow)
  [KIND→@a kinds=ratify,object] body → with kinds whitelist
  no prefix                          → (claim, None, body)
  unknown KIND                       → (claim, None, original)
"""
from __future__ import annotations

import sys
from pathlib import Path

PC_LAUNCHER_ROOT = Path(__file__).parent.parent / "pc_launcher"
if str(PC_LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(PC_LAUNCHER_ROOT))

from connectors.claude_executor.agent_loop import BridgeAgentLoop


parse = BridgeAgentLoop._parse_reply_prefix


def test_terminal_kind_only():
    assert parse("[CLAIM] hello world") == ("claim", None, "hello world")
    assert parse("[OBJECT] no") == ("object", None, "no")
    assert parse("[REACT] +1") == ("react", None, "+1")


def test_no_prefix_defaults_to_claim():
    assert parse("just a claim") == ("claim", None, "just a claim")


def test_inviting_with_unicode_arrow():
    kind, ex, body = parse("[PROPOSE→@reviewer,@alice] do this")
    assert kind == "propose"
    assert ex == {"from_actor_handles": ["@reviewer", "@alice"]}
    assert body == "do this"


def test_inviting_with_ascii_arrow():
    kind, ex, body = parse("[QUESTION->@operator] what next?")
    assert kind == "question"
    assert ex == {"from_actor_handles": ["@operator"]}
    assert body == "what next?"


def test_inviting_at_only_no_arrow():
    """Tolerates ``[KIND@a,@b]`` without an explicit arrow."""
    kind, ex, body = parse("[INVITE@bob] hi")
    assert kind == "invite"
    assert ex == {"from_actor_handles": ["@bob"]}


def test_inviting_with_kinds_whitelist():
    kind, ex, body = parse("[PROPOSE→@a kinds=ratify,object] vote please")
    assert kind == "propose"
    assert ex is not None
    assert ex["from_actor_handles"] == ["@a"]
    assert ex["kinds"] == ["ratify", "object"]
    assert body == "vote please"


def test_inviting_with_kinds_wildcard():
    kind, ex, body = parse("[QUESTION→@x kinds=*] open ended")
    assert ex is not None
    assert ex["kinds"] == ["*"]


def test_handles_de_duplicated():
    """Repeating @bob doesn't appear twice."""
    kind, ex, body = parse("[PROPOSE→@bob,@bob,@alice] x")
    assert ex is not None
    assert ex["from_actor_handles"] == ["@bob", "@alice"]


def test_unknown_kind_falls_back_to_claim():
    kind, ex, body = parse("[NOTAKIND] body")
    assert kind == "claim"
    assert ex is None
    # body should preserve the original prefix when fallback fires
    assert body == "[NOTAKIND] body"


def test_malformed_no_close_bracket():
    """Missing ``]`` → fall back, don't crash."""
    kind, ex, body = parse("[PROPOSE@a missing close")
    assert kind == "claim"
    assert ex is None


def test_skip_sentinel_handled_separately():
    """SKIP isn't a prefix — separate code path in _post_claim. Parser
    sees it as a terminal claim with body=SKIP. (The post_claim layer
    intercepts before parsing.)"""
    kind, ex, body = parse("SKIP")
    assert kind == "claim"
    assert ex is None
    assert body == "SKIP"


def test_kinds_with_extra_whitespace():
    kind, ex, body = parse("[PROPOSE→ @a , @b  kinds= ratify, object ] body")
    assert ex is not None
    assert ex["from_actor_handles"] == ["@a", "@b"]
    assert ex["kinds"] == ["ratify", "object"]


def test_terminal_overrides_intent_when_no_arrow():
    """Without arrow / @, even multi-target syntax is ignored."""
    kind, ex, body = parse("[CLAIM] reaching out to @bob and @alice in body")
    assert ex is None
    # @bob/@alice in body ≠ structured invite
    assert "@bob" in body
