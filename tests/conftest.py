from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


OPS_CURE_ROOT = Path(r"C:\Users\darkh\Projects\ops-cure")
NAS_BRIDGE_ROOT = OPS_CURE_ROOT / "nas_bridge"


class FakeThreadManager:
    def __init__(self) -> None:
        self.created_threads: list[str] = []
        self.messages: list[tuple[str, str]] = []
        self.message_store: dict[str, tuple[str, str]] = {}
        self.edited_messages: list[tuple[str, str]] = []
        self.archived_threads: list[tuple[str, str]] = []
        self.cleaned_threads: list[tuple[str, str]] = []
        self.missing_threads: set[str] = set()

    async def create_session_thread(
        self,
        *,
        guild_id: str,
        parent_channel_id: str,
        project_name: str,
        template: str,
        auto_archive_duration: int,
    ) -> str:
        del guild_id, parent_channel_id, template, auto_archive_duration
        thread_id = f"thread-{project_name}-{len(self.created_threads) + 1}"
        self.created_threads.append(thread_id)
        return thread_id

    @staticmethod
    def _embed_to_text(embed) -> str:
        parts: list[str] = []
        title = getattr(embed, "title", None)
        if title:
            parts.append(str(title))
        description = getattr(embed, "description", None)
        if description:
            parts.append(str(description))
        for field in getattr(embed, "fields", []):
            parts.append(f"{field.name}: {field.value}")
        footer = getattr(getattr(embed, "footer", None), "text", None)
        if footer:
            parts.append(str(footer))
        return "\n".join(parts)

    async def post_message(self, thread_id: str, content: str):
        self.messages.append((thread_id, content))
        message_id = f"message-{len(self.messages)}"
        self.message_store[message_id] = (thread_id, content)
        return [(message_id, content)]

    async def edit_message(self, thread_id: str, message_id: str, content: str):
        if message_id not in self.message_store:
            return None
        self.edited_messages.append((thread_id, content))
        self.message_store[message_id] = (thread_id, content)
        return (message_id, content)

    async def post_embed_message(self, thread_id: str, *, embed, content: str | None = None):
        rendered = content or self._embed_to_text(embed)
        self.messages.append((thread_id, rendered))
        message_id = f"message-{len(self.messages)}"
        self.message_store[message_id] = (thread_id, rendered)
        return (message_id, rendered)

    async def edit_embed_message(self, thread_id: str, message_id: str, *, embed, content: str | None = None):
        if message_id not in self.message_store:
            return None
        rendered = content or self._embed_to_text(embed)
        self.edited_messages.append((thread_id, rendered))
        self.message_store[message_id] = (thread_id, rendered)
        return (message_id, rendered)

    async def archive_thread(self, thread_id: str, reason: str) -> None:
        self.archived_threads.append((thread_id, reason))

    async def cleanup_thread(self, thread_id: str, reason: str) -> str:
        self.cleaned_threads.append((thread_id, reason))
        self.archived_threads.append((thread_id, reason))
        return "archived"

    async def probe_thread_state(self, thread_id: str) -> str:
        if thread_id in self.missing_threads:
            return "missing"
        if thread_id in self.created_threads:
            return "exists"
        return "unknown"


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))

    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}")

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    import app.config as config

    config.get_settings.cache_clear()

    import app.services.announcement_service as announcement_service
    import app.db as db
    import app.drift_monitor as drift_monitor
    import app.schemas as schemas
    import app.session_service as session_service
    import app.services.policy_service as policy_service
    import app.services.recovery_service as recovery_service
    import app.services.verification_service as verification_service
    import app.transcript_service as transcript_service
    import app.worker_registry as worker_registry
    import app.workflows.pause_workflow as pause_workflow
    import app.workflows.policy_workflow as policy_workflow
    import app.workflows.start_workflow as start_workflow
    from app.capabilities.execution.windows_launcher import WindowsLauncherExecutionProvider
    from app.capabilities.power.noop import NoopPowerProvider

    db.init_db()

    registry = worker_registry.WorkerRegistry(90)
    transcript = transcript_service.TranscriptService()
    thread_manager = FakeThreadManager()
    announcement_svc = announcement_service.AnnouncementService(thread_manager=thread_manager)
    drift = drift_monitor.DriftMonitor()
    session_svc = session_service.SessionService(
        registry=registry,
        thread_manager=thread_manager,
        transcript_service=transcript,
        drift_monitor=drift,
    )
    policy_svc = policy_service.PolicyService()
    power_provider = NoopPowerProvider()
    execution_provider = WindowsLauncherExecutionProvider(registry)
    recovery_svc = recovery_service.RecoveryService(
        registry=registry,
        transcript_service=transcript,
        thread_manager=thread_manager,
        announcement_service=announcement_svc,
        power_provider=power_provider,
        execution_provider=execution_provider,
        worker_stale_after_seconds=90,
        stalled_start_timeout_seconds=300,
    )
    verification_svc = verification_service.VerificationService(
        registry=registry,
        transcript_service=transcript,
        thread_manager=thread_manager,
        announcement_service=announcement_svc,
    )
    start_wf = start_workflow.StartWorkflow(
        session_service=session_svc,
        policy_service=policy_svc,
        recovery_service=recovery_svc,
        announcement_service=announcement_svc,
    )
    pause_wf = pause_workflow.PauseWorkflow(
        recovery_service=recovery_svc,
        transcript_service=transcript,
        announcement_service=announcement_svc,
    )
    policy_wf = policy_workflow.PolicyWorkflow(
        session_service=session_svc,
        policy_service=policy_svc,
        announcement_service=announcement_svc,
    )
    announcement_svc.bind_summary_provider(session_svc.get_session_summary)
    session_svc.bind_orchestration(
        policy_service=policy_svc,
        recovery_service=recovery_svc,
        start_workflow=start_wf,
        pause_workflow=pause_wf,
        policy_workflow=policy_wf,
        execution_provider=execution_provider,
        announcement_service=announcement_svc,
    )

    return SimpleNamespace(
        db=db,
        drift=drift,
        registry=registry,
        schemas=schemas,
        announcement_service=announcement_svc,
        session_service=session_svc,
        policy_service=policy_svc,
        recovery_service=recovery_svc,
        verification_service=verification_svc,
        thread_manager=thread_manager,
    )
