"""H2: bounded_match + adversarial corpus property test of session_service regex set."""
from __future__ import annotations

import random
import re
import sys
import time

import pytest

from conftest import NAS_BRIDGE_ROOT


def _import():
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    from app.security.regex_safety import (
        bounded_match,
        bounded_search,
        bounded_findall,
        bounded_sub,
        MAX_REGEX_INPUT_LEN,
    )
    return locals()


# ---------------------------------------------------------------------------
# bounded_match basic contract
# ---------------------------------------------------------------------------


def test_bounded_match_within_cap_matches():
    m = _import()
    pat = re.compile(r"^hello (\w+)$")
    res = m["bounded_match"](pat, "hello world")
    assert res is not None
    assert res.group(1) == "world"


def test_bounded_match_no_match_returns_none():
    m = _import()
    pat = re.compile(r"^hello$")
    assert m["bounded_match"](pat, "world") is None


def test_bounded_match_over_cap_returns_none_without_running_regex():
    m = _import()
    pat = re.compile(r"^x")  # would match a leading x
    big = "x" + "y" * (m["MAX_REGEX_INPUT_LEN"] + 1)
    # Despite the regex matching, we should refuse to run it.
    assert m["bounded_match"](pat, big) is None


def test_bounded_match_at_exact_cap_runs():
    m = _import()
    pat = re.compile(r"^x+$")
    cap = m["MAX_REGEX_INPUT_LEN"]
    res = m["bounded_match"](pat, "x" * cap)
    assert res is not None


def test_bounded_match_handles_none_input():
    m = _import()
    pat = re.compile(r".")
    assert m["bounded_match"](pat, None) is None


def test_bounded_match_custom_max_len():
    m = _import()
    pat = re.compile(r"^a+$")
    assert m["bounded_match"](pat, "a" * 50, max_len=10) is None
    assert m["bounded_match"](pat, "a" * 5, max_len=10) is not None


# ---------------------------------------------------------------------------
# bounded_search / findall / sub
# ---------------------------------------------------------------------------


def test_bounded_search_finds_in_middle():
    m = _import()
    pat = re.compile(r"\bT-\d+\b")
    res = m["bounded_search"](pat, "ref T-042 in body")
    assert res is not None
    assert res.group(0) == "T-042"


def test_bounded_search_over_cap_returns_none():
    m = _import()
    pat = re.compile(r"\bT-\d+\b")
    big = "noise " * 5000  # >> MAX_REGEX_INPUT_LEN
    assert m["bounded_search"](pat, big) is None


def test_bounded_findall_returns_empty_on_oversize():
    m = _import()
    pat = re.compile(r"\d+")
    big = "1 " * 10000
    assert m["bounded_findall"](pat, big) == []


def test_bounded_sub_passes_through_oversize_unchanged():
    m = _import()
    pat = re.compile(r"secret")
    big = "secret " + ("x" * (m["MAX_REGEX_INPUT_LEN"] + 100))
    out = m["bounded_sub"](pat, "[REDACTED]", big)
    # Redaction skipped — this is a security-relevant tradeoff documented
    # in the helper docstring; callers that care must check len() first.
    assert out == big


def test_bounded_sub_under_cap_redacts():
    m = _import()
    pat = re.compile(r"secret")
    out = m["bounded_sub"](pat, "[X]", "say secret here")
    assert out == "say [X] here"


# ---------------------------------------------------------------------------
# Property test: session_service regex set must terminate fast on any
# input <= cap, including adversarial corpora.
# ---------------------------------------------------------------------------


_ADVERSARIAL_TEMPLATES = [
    # repeated metacharacter-heavy strings
    "@" * 4096,
    "*" * 4096,
    "*" * 2048 + "a" * 2048,
    "**" * 2048,
    "T-" + "9" * 4000,
    "OPS:" + " " * 4000 + "type=x",
    "@a" + " " * 4000,
    "** " * 1024,
    # mixes of newline and space (DOTALL flag risk)
    "\n".join(["@bot " + "x" * 64] * 64),
    # repeated capture-group bait
    "@" + "a" * 4000 + " " + "b" * 4000,
    # alternation bait for HANDOFF/DISCUSS-shaped patterns
    "handoff " * 1000,
    "discuss " * 1000,
    # near-match boundary cases
    "@" + "a-" * 1000,
]


def _load_session_regex_set():
    """Return the actual regex objects the boundary uses."""
    import os
    os.environ.setdefault("BRIDGE_SHARED_AUTH_TOKEN", "t")
    os.environ.setdefault("BRIDGE_DISABLE_DISCORD", "true")
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    from app import session_service
    patterns = []
    for name in dir(session_service):
        obj = getattr(session_service, name)
        if isinstance(obj, re.Pattern):
            patterns.append((name, obj))
    return patterns


def test_each_session_regex_terminates_fast_on_adversarial_corpus():
    """Per-pattern, per-input ceiling: 50ms on any item in the corpus.

    bounded_match (length cap) is what makes this hold; without it,
    a backtracking pattern on a 4 KB adversarial blob can take seconds.
    """
    m = _import()
    patterns = _load_session_regex_set()
    assert patterns, "expected to discover compiled regexes in session_service"

    deadline_s = 0.05
    failures: list[str] = []
    for name, pat in patterns:
        for i, sample in enumerate(_ADVERSARIAL_TEMPLATES):
            start = time.perf_counter()
            m["bounded_match"](pat, sample)
            m["bounded_search"](pat, sample)
            elapsed = time.perf_counter() - start
            if elapsed > deadline_s:
                failures.append(
                    f"{name} sample[{i}] elapsed={elapsed * 1000:.1f}ms"
                )
    assert not failures, "regex slowdown:\n  " + "\n  ".join(failures)


def test_random_input_under_cap_terminates_fast():
    """Random fuzz: any string <= MAX_REGEX_INPUT_LEN, drawn from a
    harsh alphabet, must complete the entire session_service regex
    sweep in under 100ms total per input."""
    m = _import()
    patterns = _load_session_regex_set()
    rng = random.Random(0xDEAD)
    alphabet = "@*-_T:0123456789abc \n\t"
    deadline_s = 0.1
    for _ in range(200):
        length = rng.randint(0, m["MAX_REGEX_INPUT_LEN"])
        sample = "".join(rng.choices(alphabet, k=length))
        start = time.perf_counter()
        for name, pat in patterns:
            m["bounded_search"](pat, sample)
        elapsed = time.perf_counter() - start
        assert elapsed < deadline_s, (
            f"random sample len={length} elapsed={elapsed * 1000:.1f}ms"
        )


def test_oversized_random_input_skipped_fast():
    """Inputs above the cap must be rejected near-instantly without
    running any regex backtracking."""
    m = _import()
    patterns = _load_session_regex_set()
    big = "@" * (m["MAX_REGEX_INPUT_LEN"] * 4)
    start = time.perf_counter()
    for _ in range(50):
        for name, pat in patterns:
            assert m["bounded_match"](pat, big) is None
            assert m["bounded_search"](pat, big) is None
    elapsed = time.perf_counter() - start
    # 50 iterations × N patterns × 2 calls. Should be < 0.5s for any
    # reasonable N — len() check is O(1).
    assert elapsed < 0.5, f"oversize check too slow: {elapsed:.3f}s"
