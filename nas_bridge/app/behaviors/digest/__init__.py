"""Digest behavior — auto-summary cards on operation close + daily rollups.

Why this exists:
    Once an operation closes, its event log holds the full record but
    "what happened" requires reading 12-18 events. Future agents and
    humans want a compact card: who participated, how many speeches /
    evidence / artifacts, the opening question, the closing summary,
    duration, resolution. Generating this at close-time freezes the
    snapshot when context is fresh; daily rollups aggregate per-space.

Design:
    - DigestService.record_close(db, v2_operation_id, v2_close_event_id)
      runs in the same db session as the close itself, attaching a v2
      OperationArtifact (kind=summary, mime=application/json) inline
      via data: URI. No external storage required.
    - DigestService.compose_space_rollup(space_id, since, until) returns
      markdown summarizing every op closed in that window. Caller (cron
      / scheduler) decides where to post.

The behavior is purely additive: removing it leaves v1+v2 unaffected.
Tests cover both close-time card and rollup compose.
"""
from .service import DigestService, ARTIFACT_KIND_SUMMARY  # noqa: F401

__all__ = ["DigestService", "ARTIFACT_KIND_SUMMARY"]
