"""In-memory counters for the chat behavior protocol.

A small, dependency-free metrics surface so operators can see at a
glance "how much is this room actually doing". Counts are kept on a
plain object (no Prometheus, no statsd) and reset to zero on bridge
restart -- which is the right granularity for a single-NAS deploy.

The counters are global across all chat threads; per-thread health
information is derived live from the DB by the health endpoint
(``GET /api/chat/threads/{tid}/health``).

Future evolution: when multi-room scale appears, swap this for a
proper time-series exporter behind the same call sites. The increment
methods are deliberately stable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatRoomMetrics:
    conversations_opened: int = 0
    conversations_closed_by_resolution: Counter = field(default_factory=Counter)
    idle_warnings_by_tier: Counter = field(default_factory=Counter)
    handoffs: int = 0
    speech_by_kind: Counter = field(default_factory=Counter)
    task_claimed: int = 0
    task_heartbeat: int = 0
    task_evidence: int = 0
    task_completed: int = 0
    task_failed: int = 0

    def record_conversation_opened(self) -> None:
        self.conversations_opened += 1

    def record_conversation_closed(self, *, resolution: str) -> None:
        self.conversations_closed_by_resolution[resolution] += 1

    def record_idle_warning(self, *, tier: int) -> None:
        self.idle_warnings_by_tier[tier] += 1

    def record_handoff(self) -> None:
        self.handoffs += 1

    def record_speech(self, *, kind: str) -> None:
        self.speech_by_kind[kind] += 1

    def record_task_claimed(self) -> None:
        self.task_claimed += 1

    def record_task_heartbeat(self) -> None:
        self.task_heartbeat += 1

    def record_task_evidence(self) -> None:
        self.task_evidence += 1

    def record_task_completed(self) -> None:
        self.task_completed += 1

    def record_task_failed(self) -> None:
        self.task_failed += 1

    def snapshot(self) -> dict[str, Any]:
        """JSON-serializable snapshot for the health endpoint."""
        return {
            "conversations_opened": self.conversations_opened,
            "conversations_closed_by_resolution": dict(self.conversations_closed_by_resolution),
            "idle_warnings_by_tier": {str(k): v for k, v in self.idle_warnings_by_tier.items()},
            "handoffs": self.handoffs,
            "speech_by_kind": dict(self.speech_by_kind),
            "task": {
                "claimed": self.task_claimed,
                "heartbeat": self.task_heartbeat,
                "evidence": self.task_evidence,
                "completed": self.task_completed,
                "failed": self.task_failed,
            },
        }
