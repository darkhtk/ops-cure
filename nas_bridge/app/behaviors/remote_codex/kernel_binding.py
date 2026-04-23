"""Future kernel binding hook for remote_codex.

The canonical remote_codex behavior should eventually project its task/activity
truth into generic Space / Actor / Event views. For now we expose an empty
binding so the package has a stable landing zone without changing live behavior.
"""

from __future__ import annotations

from ...kernel.bindings import KernelBehaviorBinding


def build_remote_codex_kernel_binding() -> KernelBehaviorBinding:
    return KernelBehaviorBinding(behavior_id="remote_codex")
