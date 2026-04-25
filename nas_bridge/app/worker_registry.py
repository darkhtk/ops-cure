from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import selectinload

from .db import session_scope
from .behaviors.workflow.schemas import ProjectManifest
from .kernel.models import LauncherCatalogEntryModel, LauncherRecordModel

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

    def register_projects(
        self,
        launcher_id: str,
        hostname: str,
        projects: list[ProjectManifest],
    ) -> None:
        manifest_map = {project.profile_name: project for project in projects}
        now = utcnow()
        with session_scope() as db:
            launcher = db.scalar(
                select(LauncherRecordModel)
                .options(selectinload(LauncherRecordModel.catalog_entries))
                .where(LauncherRecordModel.launcher_id == launcher_id),
            )
            if launcher is None:
                launcher = LauncherRecordModel(
                    launcher_id=launcher_id,
                    hostname=hostname,
                    status="online",
                    last_seen_at=now,
                )
                db.add(launcher)
                db.flush()
            else:
                # Detect cross-machine launcher_id collision: if the same
                # launcher_id is currently held by a different hostname AND
                # that other host has heartbeated within the stale-after
                # window, the new register effectively yanks ownership away
                # from a live launcher. Keep the takeover to preserve today's
                # behavior, but log a WARNING so an operator can spot the
                # duplicate and renumber one of the launchers.
                if launcher.hostname != hostname:
                    last_seen = self._coerce_aware(launcher.last_seen_at)
                    age = (now - last_seen) if last_seen is not None else self._stale_after
                    if age < self._stale_after:
                        LOGGER.warning(
                            "launcher_id collision: %s currently held by %s "
                            "(last seen %s, %.1fs ago); takeover by %s",
                            launcher_id,
                            launcher.hostname,
                            last_seen,
                            age.total_seconds(),
                            hostname,
                        )
                    else:
                        LOGGER.info(
                            "launcher_id %s reclaimed from stale host %s by %s",
                            launcher_id,
                            launcher.hostname,
                            hostname,
                        )
                launcher.hostname = hostname
                launcher.status = "online"
                launcher.last_seen_at = now

            db.execute(
                delete(LauncherCatalogEntryModel).where(
                    LauncherCatalogEntryModel.launcher_id == launcher_id,
                ),
            )
            for profile_name, manifest in manifest_map.items():
                db.add(
                    LauncherCatalogEntryModel(
                        launcher_id=launcher_id,
                        profile_name=profile_name,
                        manifest_json=json.dumps(manifest.model_dump(), ensure_ascii=False),
                    ),
                )
        LOGGER.info(
            "Launcher %s registered %s project manifests",
            launcher_id,
            len(projects),
        )

    def is_launcher_id_active_elsewhere(
        self,
        *,
        launcher_id: str,
        hostname: str,
    ) -> bool:
        """Returns True if ``launcher_id`` is currently registered to a
        DIFFERENT hostname and the last heartbeat is within the stale-after
        window. Caller (typically a fresh runner about to register) can use
        this to bail out before clobbering another live launcher.
        """
        with session_scope() as db:
            launcher = db.scalar(
                select(LauncherRecordModel).where(LauncherRecordModel.launcher_id == launcher_id),
            )
            if launcher is None:
                return False
            if launcher.hostname == hostname:
                return False
            last_seen = self._coerce_aware(launcher.last_seen_at)
            if last_seen is None:
                return False
            return (utcnow() - last_seen) < self._stale_after

    def prune_stale_launchers(self) -> None:
        cutoff = utcnow() - self._stale_after
        with session_scope() as db:
            stale_launchers = list(
                db.scalars(
                    select(LauncherRecordModel).where(LauncherRecordModel.last_seen_at < cutoff),
                ),
            )
            for launcher in stale_launchers:
                if launcher.status != "stale":
                    LOGGER.warning("Marking launcher registry entry stale: %s", launcher.launcher_id)
                    launcher.status = "stale"

    def get_project(self, project_name: str) -> ProjectManifest | None:
        self.prune_stale_launchers()
        with session_scope() as db:
            entry = db.scalar(
                select(LauncherCatalogEntryModel)
                .join(LauncherRecordModel, LauncherCatalogEntryModel.launcher_id == LauncherRecordModel.launcher_id)
                .where(LauncherCatalogEntryModel.profile_name == project_name)
                .where(LauncherRecordModel.status == "online")
                .order_by(LauncherRecordModel.last_seen_at.desc()),
            )
            return self._manifest_from_entry(entry)

    def find_launcher_for_project(self, project_name: str) -> LauncherRecord | None:
        self.prune_stale_launchers()
        with session_scope() as db:
            launcher = db.scalar(
                select(LauncherRecordModel)
                .join(LauncherCatalogEntryModel, LauncherCatalogEntryModel.launcher_id == LauncherRecordModel.launcher_id)
                .options(selectinload(LauncherRecordModel.catalog_entries))
                .where(LauncherCatalogEntryModel.profile_name == project_name)
                .where(LauncherRecordModel.status == "online")
                .order_by(LauncherRecordModel.last_seen_at.desc()),
            )
            if launcher is None:
                return None
            return self._to_record(launcher)

    def get_projects_for_launcher(self, launcher_id: str) -> dict[str, ProjectManifest]:
        self.prune_stale_launchers()
        with session_scope() as db:
            launcher = db.scalar(
                select(LauncherRecordModel)
                .options(selectinload(LauncherRecordModel.catalog_entries))
                .where(LauncherRecordModel.launcher_id == launcher_id),
            )
            if launcher is None:
                return {}
            launcher.status = "online"
            launcher.last_seen_at = utcnow()
            return self._project_map(launcher.catalog_entries)

    def list_project_names(self) -> list[str]:
        self.prune_stale_launchers()
        with session_scope() as db:
            rows = db.scalars(
                select(distinct(LauncherCatalogEntryModel.profile_name))
                .join(LauncherRecordModel, LauncherCatalogEntryModel.launcher_id == LauncherRecordModel.launcher_id)
                .where(LauncherRecordModel.status == "online"),
            )
            return sorted(rows)

    def active_launcher_count(self) -> int:
        self.prune_stale_launchers()
        with session_scope() as db:
            return int(
                db.scalar(
                    select(func.count())
                    .select_from(LauncherRecordModel)
                    .where(LauncherRecordModel.status == "online"),
                )
                or 0,
            )

    def tracked_project_count(self) -> int:
        self.prune_stale_launchers()
        with session_scope() as db:
            return int(
                db.scalar(
                    select(func.count())
                    .select_from(LauncherCatalogEntryModel)
                    .join(LauncherRecordModel, LauncherCatalogEntryModel.launcher_id == LauncherRecordModel.launcher_id)
                    .where(LauncherRecordModel.status == "online"),
                )
                or 0,
            )

    @staticmethod
    def _coerce_aware(value: datetime | None) -> datetime | None:
        """SQLite drivers can hand back naive datetimes even when the column
        is declared with ``DateTime(timezone=True)``. Treat any naive value
        as UTC so timedelta arithmetic against ``utcnow()`` doesn't blow up
        with TypeError when comparing aware vs naive datetimes.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @staticmethod
    def _manifest_from_entry(entry: LauncherCatalogEntryModel | None) -> ProjectManifest | None:
        if entry is None:
            return None
        return ProjectManifest.model_validate(json.loads(entry.manifest_json))

    @classmethod
    def _project_map(cls, entries: list[LauncherCatalogEntryModel]) -> dict[str, ProjectManifest]:
        manifests: dict[str, ProjectManifest] = {}
        for entry in entries:
            manifest = cls._manifest_from_entry(entry)
            if manifest is not None:
                manifests[entry.profile_name] = manifest
        return manifests

    @classmethod
    def _to_record(cls, launcher: LauncherRecordModel) -> LauncherRecord:
        return LauncherRecord(
            launcher_id=launcher.launcher_id,
            hostname=launcher.hostname,
            projects=cls._project_map(list(launcher.catalog_entries)),
            last_seen_at=launcher.last_seen_at,
        )
