"""Adversarial-robustness bounds for the protocol perimeter.

These modules enforce *transport-level* and *parser-level* limits that
the protocol kernel assumes have already been applied. Without them
the rubric audit (axis H) finds no enforcement evidence: the kernel
trusts that input was bounded somewhere upstream, but no upstream
guarantees it.

Public surface:

  - ``BodySizeLimitMiddleware``    — ASGI middleware capping request bodies.
  - ``RequestTimeoutMiddleware``   — Starlette middleware bounding handler runtime.
  - ``walk_json_depth``            — guard against deeply-nested free-form JSON.
  - ``bounded_match``              — input-length pre-check before regex match.
  - ``BoundsConfig``               — settings snapshot consumed by the middlewares.
"""

from .bounds import (
    BodySizeLimitMiddleware,
    BoundsConfig,
    RequestTimeoutMiddleware,
    walk_json_depth,
)
from .regex_safety import (
    MAX_REGEX_INPUT_LEN,
    bounded_findall,
    bounded_fullmatch,
    bounded_match,
    bounded_search,
    bounded_sub,
)

__all__ = [
    "BodySizeLimitMiddleware",
    "BoundsConfig",
    "RequestTimeoutMiddleware",
    "walk_json_depth",
    "bounded_findall",
    "bounded_fullmatch",
    "bounded_match",
    "bounded_search",
    "bounded_sub",
    "MAX_REGEX_INPUT_LEN",
]
