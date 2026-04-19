from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ArtifactSnapshot:
    workspace_ready: bool = False
    state_label: str | None = None
    state_updated_at: datetime | None = None
    current_task_state: str | None = None
    current_task_id: str | None = None
    current_task_updated_at: datetime | None = None
    latest_artifact_at: datetime | None = None
    latest_artifact_path: str | None = None


@dataclass(slots=True)
class DriftEvaluation:
    drift_state: str
    drift_reason: str | None = None
    workspace_ready: bool | None = None
    last_artifact_at: datetime | None = None
    last_artifact_path: str | None = None
    current_task_id: str | None = None
    current_task_state: str | None = None


@dataclass(slots=True)
class WorkerDriftRecord:
    session_id: str
    agent_name: str
    worker_id: str
    worker_status: str
    registered_at: datetime
    last_heartbeat_at: datetime
    artifact_snapshot: ArtifactSnapshot | None = None


class DriftMonitor:
    def __init__(
        self,
        *,
        artifact_stale_after_seconds: int = 180,
        workspace_grace_seconds: int = 45,
    ) -> None:
        self._artifact_stale_after = timedelta(seconds=max(30, artifact_stale_after_seconds))
        self._workspace_grace = timedelta(seconds=max(10, workspace_grace_seconds))
        self._records: dict[tuple[str, str], WorkerDriftRecord] = {}
        self._lock = Lock()

    def register_worker(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
        worker_status: str = "starting",
    ) -> None:
        now = utcnow()
        key = (session_id, agent_name)
        with self._lock:
            existing = self._records.get(key)
            self._records[key] = WorkerDriftRecord(
                session_id=session_id,
                agent_name=agent_name,
                worker_id=worker_id,
                worker_status=worker_status,
                registered_at=existing.registered_at if existing is not None else now,
                last_heartbeat_at=now,
                artifact_snapshot=existing.artifact_snapshot if existing and existing.worker_id == worker_id else None,
            )

    def record_heartbeat(
        self,
        *,
        session_id: str,
        agent_name: str,
        worker_id: str,
        worker_status: str,
        artifact_snapshot: ArtifactSnapshot | None,
    ) -> None:
        now = utcnow()
        key = (session_id, agent_name)
        with self._lock:
            record = self._records.get(key)
            if record is None or record.worker_id != worker_id:
                record = WorkerDriftRecord(
                    session_id=session_id,
                    agent_name=agent_name,
                    worker_id=worker_id,
                    worker_status=worker_status,
                    registered_at=now,
                    last_heartbeat_at=now,
                    artifact_snapshot=artifact_snapshot,
                )
                self._records[key] = record
                return

            record.worker_status = worker_status
            record.last_heartbeat_at = now
            record.artifact_snapshot = artifact_snapshot

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            stale_keys = [key for key in self._records if key[0] == session_id]
            for key in stale_keys:
                self._records.pop(key, None)

    def evaluate_agent(
        self,
        *,
        session_id: str,
        agent_name: str,
        agent_status: str,
        worker_id: str | None,
    ) -> DriftEvaluation:
        now = utcnow()
        with self._lock:
            record = self._records.get((session_id, agent_name))

        if agent_status == "offline":
            return DriftEvaluation(drift_state="unknown", drift_reason="Worker is offline.")
        if record is None or worker_id is None or record.worker_id != worker_id:
            return DriftEvaluation(drift_state="unknown", drift_reason="No drift telemetry received yet.")

        snapshot = record.artifact_snapshot
        if snapshot is None:
            if now - record.registered_at > self._workspace_grace and agent_status not in {"starting"}:
                return DriftEvaluation(
                    drift_state="drift",
                    drift_reason="Heartbeat is alive but no artifact telemetry has been received.",
                )
            return DriftEvaluation(drift_state="warning", drift_reason="Waiting for the first artifact heartbeat.")

        last_artifact_at = snapshot.latest_artifact_at or snapshot.current_task_updated_at or snapshot.state_updated_at
        base = DriftEvaluation(
            drift_state="ok",
            workspace_ready=snapshot.workspace_ready,
            last_artifact_at=last_artifact_at,
            last_artifact_path=snapshot.latest_artifact_path,
            current_task_id=snapshot.current_task_id,
            current_task_state=snapshot.current_task_state or snapshot.state_label,
        )

        if not snapshot.workspace_ready:
            if now - record.registered_at > self._workspace_grace and agent_status not in {"starting"}:
                base.drift_state = "drift"
                base.drift_reason = "Heartbeat is alive but the session workspace is missing."
            else:
                base.drift_state = "warning"
                base.drift_reason = "Session workspace is still being created."
            return base

        if last_artifact_at is None:
            if now - record.registered_at > self._workspace_grace and agent_status not in {"starting"}:
                base.drift_state = "drift"
                base.drift_reason = "Heartbeat is alive but no shared markdown artifact has been written."
            else:
                base.drift_state = "warning"
                base.drift_reason = "Waiting for the first shared artifact write."
            return base

        artifact_age = now - last_artifact_at
        if agent_status in {"busy", "restarting"} and artifact_age > self._artifact_stale_after:
            base.drift_state = "drift"
            base.drift_reason = (
                "Heartbeat is alive but no shared artifact changed while the worker stayed busy "
                f"for {int(artifact_age.total_seconds())}s."
            )
            return base

        current_task_state = (snapshot.current_task_state or snapshot.state_label or "").lower()
        if agent_status == "idle" and current_task_state == "in_progress" and artifact_age > self._artifact_stale_after:
            base.drift_state = "drift"
            base.drift_reason = (
                "Worker is idle but `CURRENT_TASK.md` still reports in-progress work with no fresh artifact update."
            )
            return base

        return base
