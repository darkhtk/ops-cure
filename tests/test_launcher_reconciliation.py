from __future__ import annotations

from pathlib import Path

import yaml


def _write_sample_project(projects_dir: Path) -> Path:
    project_dir = projects_dir / "sample"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_file = project_dir / "project.yaml"
    project_file.write_text(
        yaml.safe_dump(
            {
                "profile_name": "sample",
                "default_target_name": "sample",
                "default_workdir": r"C:\Users\darkh\Projects",
                "guild_id": "guild-1",
                "parent_channel_id": "parent-1",
                "allowed_user_ids": ["user-1"],
                "bridge": {
                    "base_url": "http://127.0.0.1:18080",
                    "auth_token_env": "BRIDGE_TOKEN",
                },
                "agents": [
                    {
                        "name": "planner",
                        "cli": "claude",
                        "role": "planning",
                        "prompt_file": "prompts/planner.md",
                        "default": True,
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return project_file


class DummyProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if not self.terminated and not self.killed else 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: int | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_launcher_reconciles_external_workers(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "test-token")
    projects_dir = tmp_path / "projects"
    _write_sample_project(projects_dir)

    from pc_launcher.launcher import ExternalWorkerProcess, LauncherDaemon

    daemon = LauncherDaemon(projects_dir=projects_dir, launcher_id="homedev")
    terminated: list[tuple[int, str]] = []
    orphan = ExternalWorkerProcess(
        pid=321,
        session_id="old-session",
        agent_name="planner",
        worker_id="worker-old",
        project_file=Path(r"C:\Users\darkh\Projects\ops-cure\pc_launcher\projects\ulalacheese\project.yaml"),
        command_line="python cli_worker.py --launcher-id homedev",
    )
    monkeypatch.setattr(daemon, "_list_launcher_worker_processes", lambda: [orphan])
    monkeypatch.setattr(
        daemon,
        "_terminate_external_worker_process",
        lambda worker, reason: terminated.append((worker.pid, reason)),
    )

    daemon._reconcile_external_workers()

    assert terminated == [
        (321, "stale external worker uses an unknown or missing profile file"),
    ]


def test_launcher_reconciles_managed_workers_when_session_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "test-token")
    projects_dir = tmp_path / "projects"
    project_file = _write_sample_project(projects_dir)

    from pc_launcher.launcher import LauncherDaemon, ManagedWorker

    daemon = LauncherDaemon(projects_dir=projects_dir, launcher_id="homedev")
    managed_process = DummyProcess(pid=654)
    managed = ManagedWorker(
        process=managed_process,
        session_id="session-1",
        agent_name="planner",
        worker_id="worker-1",
        project_file=project_file,
    )
    daemon._managed_workers[("session-1", "planner")] = managed
    monkeypatch.setattr(
        daemon._bridge_client,
        "get_session",
        lambda session_id: {
            "id": session_id,
            "status": "closed",
            "desired_status": "closed",
            "agents": [],
        },
    )

    daemon._reconcile_managed_workers_with_bridge()

    assert managed_process.terminated is True
    assert ("session-1", "planner") not in daemon._managed_workers


def test_launcher_reconciles_managed_workers_when_bridge_reassigns_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "test-token")
    projects_dir = tmp_path / "projects"
    project_file = _write_sample_project(projects_dir)

    from pc_launcher.launcher import LauncherDaemon, ManagedWorker

    daemon = LauncherDaemon(projects_dir=projects_dir, launcher_id="homedev")
    managed_process = DummyProcess(pid=655)
    managed = ManagedWorker(
        process=managed_process,
        session_id="session-2",
        agent_name="planner",
        worker_id="worker-old",
        project_file=project_file,
    )
    daemon._managed_workers[("session-2", "planner")] = managed
    monkeypatch.setattr(
        daemon._bridge_client,
        "get_session",
        lambda session_id: {
            "id": session_id,
            "status": "ready",
            "desired_status": "ready",
            "agents": [
                {
                    "agent_name": "planner",
                    "worker_id": "worker-new",
                },
            ],
        },
    )

    daemon._reconcile_managed_workers_with_bridge()

    assert managed_process.terminated is True
    assert ("session-2", "planner") not in daemon._managed_workers
