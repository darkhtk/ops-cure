from __future__ import annotations

from .base import PowerActionResult, PowerProvider, PowerTarget


class RoutedPowerProvider(PowerProvider):
    provider_name = "routed"

    def __init__(self, providers: list[PowerProvider]) -> None:
        self._providers = {provider.provider_name: provider for provider in providers}

    def wake(self, target: PowerTarget) -> PowerActionResult:
        provider = self._providers.get(target.provider)
        if provider is None:
            return PowerActionResult(
                state="unknown",
                detail=f"No power provider is configured for `{target.provider}`.",
            )
        return provider.wake(target)

    def status(self, target: PowerTarget) -> PowerActionResult:
        provider = self._providers.get(target.provider)
        if provider is None:
            return PowerActionResult(
                state="unknown",
                detail=f"No power provider is configured for `{target.provider}`.",
            )
        return provider.status(target)
