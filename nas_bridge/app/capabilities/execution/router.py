from __future__ import annotations

from .base import ExecutionProvider, ExecutionStatus, ExecutionTarget


class RoutedExecutionProvider(ExecutionProvider):
    provider_name = "routed"

    def __init__(self, providers: list[ExecutionProvider]) -> None:
        self._providers = {provider.provider_name: provider for provider in providers}

    def status_for_project(self, *, project_name: str, target: ExecutionTarget) -> ExecutionStatus:
        provider = self._providers.get(target.provider)
        if provider is None:
            return ExecutionStatus(
                state="unknown",
                detail=f"No execution provider is configured for `{target.provider}`.",
            )
        return provider.status_for_project(project_name=project_name, target=target)

    def request_start(self, *, project_name: str, target: ExecutionTarget) -> ExecutionStatus:
        provider = self._providers.get(target.provider)
        if provider is None:
            return ExecutionStatus(
                state="unknown",
                detail=f"No execution provider is configured for `{target.provider}`.",
            )
        return provider.request_start(project_name=project_name, target=target)
