"""Input-length pre-check for regex application on untrusted text.

The cheapest, most-portable defense against ReDoS in the boundary
regex set (``session_service.py``, ``transcript_service.py``) is to
*never feed the regex more than N characters*. Catastrophic
backtracking grows with input length, so capping length caps the
worst case independent of the regex itself.

This is not as strong as switching to RE2/regex2, but it (a) requires
no dependency, (b) covers the entire regex surface uniformly, (c) is
trivial to property-test.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("opscure.security.regex_safety")

# Cap chosen so a Discord message (4 KB max) and typical transcript
# lines fit comfortably while a 100 KB adversarial blob does not.
# Tunable per-call site if a legit caller needs more.
MAX_REGEX_INPUT_LEN = 8192


def bounded_match(
    pattern: re.Pattern,
    text: str,
    *,
    max_len: int = MAX_REGEX_INPUT_LEN,
) -> re.Match | None:
    """Run ``pattern.match`` only if ``len(text) <= max_len``.

    Returns the Match object on a hit, ``None`` on no match OR on
    over-length input. Over-length is logged once per call site so
    operators can tell a real "no match" from "skipped due to size".

    Use ``bounded_search`` for ``pattern.search``-shape callers.
    """
    if text is None:
        return None
    if len(text) > max_len:
        logger.warning(
            "regex.skipped_oversized pattern=%r len=%d cap=%d",
            pattern.pattern[:64], len(text), max_len,
        )
        return None
    return pattern.match(text)


def bounded_fullmatch(
    pattern: re.Pattern,
    text: str,
    *,
    max_len: int = MAX_REGEX_INPUT_LEN,
) -> re.Match | None:
    """Counterpart of ``bounded_match`` for ``pattern.fullmatch``."""
    if text is None:
        return None
    if len(text) > max_len:
        logger.warning(
            "regex.skipped_oversized pattern=%r len=%d cap=%d",
            pattern.pattern[:64], len(text), max_len,
        )
        return None
    return pattern.fullmatch(text)


def bounded_search(
    pattern: re.Pattern,
    text: str,
    *,
    max_len: int = MAX_REGEX_INPUT_LEN,
) -> re.Match | None:
    """Counterpart of ``bounded_match`` for ``pattern.search``."""
    if text is None:
        return None
    if len(text) > max_len:
        logger.warning(
            "regex.skipped_oversized pattern=%r len=%d cap=%d",
            pattern.pattern[:64], len(text), max_len,
        )
        return None
    return pattern.search(text)


def bounded_findall(
    pattern: re.Pattern,
    text: str,
    *,
    max_len: int = MAX_REGEX_INPUT_LEN,
) -> list:
    """Counterpart of ``bounded_match`` for ``pattern.findall``."""
    if text is None:
        return []
    if len(text) > max_len:
        logger.warning(
            "regex.skipped_oversized pattern=%r len=%d cap=%d",
            pattern.pattern[:64], len(text), max_len,
        )
        return []
    return pattern.findall(text)


def bounded_sub(
    pattern: re.Pattern,
    replacement: str,
    text: str,
    *,
    max_len: int = MAX_REGEX_INPUT_LEN,
) -> str:
    """Counterpart of ``bounded_match`` for ``pattern.sub``.

    On over-length input, returns the original text unchanged (no
    redaction applied). Caller should treat this as "redaction skipped"
    for security-sensitive use (e.g., transcript redaction must check
    ``len(text)`` separately if it cares).
    """
    if text is None:
        return ""
    if len(text) > max_len:
        logger.warning(
            "regex.skipped_oversized pattern=%r len=%d cap=%d",
            pattern.pattern[:64], len(text), max_len,
        )
        return text
    return pattern.sub(replacement, text)
