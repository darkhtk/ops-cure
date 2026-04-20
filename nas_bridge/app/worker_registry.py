from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .schemas import ProjectManifest

LOGGER = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class LauncherRecord:
    launcher_id: str
    hostname: str
    projects: dict[str, ProjectManifest] = field(default_factory=dict)
    last_seen_at: datetime = field(default_factory=utcnow)


class WorkerRegistry:
    def __init__(self, stale_after_seconds: int) -> None:
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._launchers: dict[str, LauncherRecord] = {}

    def register_projects(
        self,
        launcher_id: str,
        hostname: str,
        projects: list[ProjectManifest],
    ) -> None:
        manifest_map = {project.project_name: project for project in projects}
        self._launchers[launcher_id] = LauncherRecord(
            launcher_id=launcher_id,
            hostname=hostname,
            projects=manifest_map,
            last_seen_at=utcnow(),
        )
        LOGGER.info(
            "Launcher %s registered %s project manifests",
            launcher_id,
            len(projects),
        )

    def prune_stale_launchers(self) -> None:
        now = utcnow()
        stale_ids = [
            launcher_id
            for launcher_id, record in self._launchers.items()
            if now - record.last_seen_at > self._stale_after
        ]
        for launcher_id in stale_ids:
            LOGGER.warning("Pruning stale launcher registry entry: %s", launcher_id)
            self._launchers.pop(launcher_id, None)

    def get_project(self, project_name: str) -> ProjectManifest | None:
        self.prune_stale_launchers()
        for record in self._launchers.values():
            project = record.projects.get(project_name)
            if project:
                return project
        return None

    def find_launcher_for_project(self, project_name: str) -> LauncherRecord | None:
        self.prune_stale_launchers()
        for record in self._launchers.values():
            if project_name in record.projects:
                return record
        return None

    def get_projects_for_launcher(self, launcher_id: str) -> dict[str, ProjectManifest]:
        self.prune_stale_launchers()
        record = self._launchers.get(launcher_id)
        if record is None:
            return {}
        record.last_seen_at = utcnow()
        return record.projects

    def list_project_names(self) -> list[str]:
        self.prune_stale_launchers()
        names: set[str] = set()
        for record in self._launchers.values():
            names.update(record.projects.keys())
        return sorted(names)

    def active_launcher_count(self) -> int:
        self.prune_stale_launchers()
        return len(self._launchers)

    def tracked_project_count(self) -> int:
        self.prune_stale_launchers()
        return sum(len(record.projects) for record in self._launchers.values())
