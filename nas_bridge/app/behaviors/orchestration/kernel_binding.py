"""Kernel binding for the public orchestration behavior."""

from __future__ import annotations

from ...kernel.bindings import KernelBehaviorBinding
from ..workflow.kernel_binding import WorkflowKernelProvider


class OrchestrationKernelProvider(WorkflowKernelProvider):
    """Public orchestration provider backed by the legacy workflow kernel logic."""


def build_orchestration_kernel_binding() -> KernelBehaviorBinding:
    provider = OrchestrationKernelProvider()
    return KernelBehaviorBinding(
        behavior_id="orchestration",
        space_provider=provider,
        actor_provider=provider,
        event_provider=provider,
    )


__all__ = ["OrchestrationKernelProvider", "build_orchestration_kernel_binding"]
