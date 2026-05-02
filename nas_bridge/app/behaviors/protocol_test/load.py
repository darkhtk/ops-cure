"""Load scenarios — N ops × M events through ScenarioDriver.

Goal: produce real numbers (events/sec, wall time, peak memory) for
the in-process protocol stack so we can spot perf regressions and
discover where the bridge breaks under volume.

Output is ``LoadObservation`` -- structurally similar to the
single-op ``ProtocolObservation`` but aggregated across many ops.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ...kernel.storage import session_scope
from ...kernel.v2 import V2Repository
from ...kernel.v2.models import OperationV2Model

from .driver import ScenarioDriver, PersonaSpec, ProtocolObservation
from .personas import (
    CuriousJuniorBrain,
    HelpfulSpecialistBrain,
    DecisiveOperatorBrain,
)


def _peak_memory_mb() -> float | None:
    """Report process RSS in MB. Returns None when psutil not available
    (we don't add it as a hard dep; the metric is best-effort)."""
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    try:
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None


@dataclass
class LoadObservation:
    n_ops: int
    n_events_total: int
    wall_seconds: float
    events_per_second: float
    rounds_to_quiesce: int
    hit_round_cap: bool
    state_distribution: dict[str, int]
    closed_ops: int
    open_ops: int
    memory_delta_mb: float | None
    per_op_avg_events: float
    error_count: int = 0
    notes: list[str] = field(default_factory=list)


class LoadScenarioRunner:
    """Build N ops, drain broker, snapshot system-wide state."""

    def __init__(
        self,
        *,
        chat_service,
        broker,
        n_ops: int = 50,
        events_per_op: int = 3,
        max_rounds: int = 100,
    ) -> None:
        self._chat = chat_service
        self._broker = broker
        self._n_ops = n_ops
        self._events_per_op = events_per_op
        self._max_rounds = max_rounds

    def run_inquiry_load(self) -> LoadObservation:
        """N inquiries with operator/specialist/junior personas. Each
        inquiry seeds a question; persona reactions run to quiescence;
        operator closes after threshold."""
        d = ScenarioDriver(
            chat_service=self._chat,
            broker=self._broker,
            personas=[
                PersonaSpec(CuriousJuniorBrain),
                PersonaSpec(HelpfulSpecialistBrain),
                PersonaSpec(
                    DecisiveOperatorBrain,
                    init_kwargs={"close_threshold": self._events_per_op},
                ),
            ],
            max_rounds=self._max_rounds,
        )

        memory_before = _peak_memory_mb()

        # Open N inquiry ops on a single thread (shares discord_thread_id;
        # all ops live in the same chat thread but are independent v2 ops).
        thread = d.make_thread(suffix="load")
        op_ids: list[str] = []
        seed_failures = 0
        for i in range(self._n_ops):
            try:
                op_id = d.open_inquiry(
                    opener_handle="@operator",
                    addressed_to_handle="@helpful-specialist",
                    title=f"load-op-{i}",
                    discord_thread_id=thread,
                    extra_participants=["@curious-junior"],
                )
                d.post_speech(
                    operation_id=op_id,
                    actor_handle="@operator",
                    kind="question",
                    text=f"question for op {i}",
                    addressed_to_handle="@helpful-specialist",
                )
                op_ids.append(op_id)
            except Exception:  # noqa: BLE001 -- tally + continue
                seed_failures += 1

        t0 = time.perf_counter()
        rounds = d.process_pending(max_rounds=self._max_rounds)
        wall = time.perf_counter() - t0

        memory_after = _peak_memory_mb()
        memory_delta = (
            (memory_after - memory_before)
            if (memory_after is not None and memory_before is not None)
            else None
        )

        # Aggregate state distribution + event counts via repo
        repo = V2Repository()
        state_dist: dict[str, int] = {}
        n_events = 0
        with session_scope() as db:
            stmt = (
                select(OperationV2Model)
                .where(OperationV2Model.id.in_(op_ids))
            )
            for op in db.scalars(stmt):
                state_dist[op.state] = state_dist.get(op.state, 0) + 1
                events = repo.list_events(db, operation_id=op.id, limit=1000)
                n_events += len(events)

        eps = (n_events / wall) if wall > 0 else 0.0
        notes: list[str] = []
        if seed_failures:
            notes.append(f"{seed_failures} op-open seeds failed")
        if rounds >= self._max_rounds:
            notes.append("hit round cap -- protocol did NOT quiesce")

        return LoadObservation(
            n_ops=len(op_ids),
            n_events_total=n_events,
            wall_seconds=wall,
            events_per_second=eps,
            rounds_to_quiesce=rounds,
            hit_round_cap=(rounds >= self._max_rounds),
            state_distribution=state_dist,
            closed_ops=state_dist.get("closed", 0),
            open_ops=state_dist.get("open", 0),
            memory_delta_mb=memory_delta,
            per_op_avg_events=(n_events / len(op_ids)) if op_ids else 0.0,
            notes=notes,
        )
