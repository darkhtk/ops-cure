from __future__ import annotations

import asyncio


def test_state_service_mirrors_command_publish_to_kernel_broker(app_env):
    """Command publishes also flow through the kernel subscription broker
    on the synthetic ``remote_codex.machine:{id}`` space, so device-side
    runners can subscribe via /api/events/spaces/.../stream instead of
    polling claim-next.
    """
    from app.behaviors.remote_codex.state_service import (
        REMOTE_CODEX_MACHINE_SPACE_PREFIX,
        RemoteCodexStateService,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker

    broker = InProcessSubscriptionBroker()
    state_service = RemoteCodexStateService(kernel_subscription_broker=broker)

    space_id = f"{REMOTE_CODEX_MACHINE_SPACE_PREFIX}homedev"
    subscription = broker.subscribe(space_id=space_id)

    state_service._publish(
        "homedev",
        "thread-A",
        {
            "kind": "command",
            "command": {
                "commandId": "cmd-1",
                "machineId": "homedev",
                "threadId": "thread-A",
                "status": "queued",
                "type": "turn.start",
            },
        },
    )

    received = asyncio.run(subscription.next_event(timeout_seconds=1.0))
    assert received is not None
    assert received.space_id == space_id
    assert received.event.id == "cmd-1"
    assert received.event.kind == "remote_codex.command.queued"


def test_state_service_does_not_mirror_non_command_payloads(app_env):
    from app.behaviors.remote_codex.state_service import (
        REMOTE_CODEX_MACHINE_SPACE_PREFIX,
        RemoteCodexStateService,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker

    broker = InProcessSubscriptionBroker()
    state_service = RemoteCodexStateService(kernel_subscription_broker=broker)

    subscription = broker.subscribe(
        space_id=f"{REMOTE_CODEX_MACHINE_SPACE_PREFIX}homedev",
    )

    state_service._publish(
        "homedev",
        "thread-A",
        {"kind": "machine", "machine": {"machineId": "homedev"}},
    )
    state_service._publish(
        "homedev",
        "thread-A",
        {"kind": "snapshot"},
    )

    received = asyncio.run(subscription.next_event(timeout_seconds=0.2))
    assert received is None


def test_state_service_works_without_kernel_broker(app_env):
    """When no kernel subscription broker is wired up the state service
    must still handle publishes without raising — that's the legacy
    standalone configuration that other tests already rely on.
    """
    from app.behaviors.remote_codex.state_service import RemoteCodexStateService

    state_service = RemoteCodexStateService()
    state_service._publish(
        "homedev",
        "thread-A",
        {"kind": "command", "command": {"commandId": "cmd-1", "status": "queued"}},
    )


def test_kernel_provider_returns_summary_for_machine_space(app_env):
    from app.behaviors.remote_codex.kernel_binding import RemoteCodexKernelProvider
    from app.behaviors.remote_codex.state_service import REMOTE_CODEX_MACHINE_SPACE_PREFIX

    provider = RemoteCodexKernelProvider()
    summary = provider.get_space(space_id=f"{REMOTE_CODEX_MACHINE_SPACE_PREFIX}homedev")

    assert summary is not None
    assert summary.id == f"{REMOTE_CODEX_MACHINE_SPACE_PREFIX}homedev"
    assert summary.domain_type == "remote_codex.machine"
    assert summary.metadata.get("machine_id") == "homedev"


def test_kernel_provider_ignores_non_machine_space_ids(app_env):
    from app.behaviors.remote_codex.kernel_binding import RemoteCodexKernelProvider

    provider = RemoteCodexKernelProvider()

    assert provider.get_space(space_id="thread-1") is None
    assert provider.get_space(space_id="chat:thread-A") is None
    assert provider.get_space_by_thread(thread_id="thread-1") is None
