from __future__ import annotations

from .base import PowerActionResult, PowerProvider, PowerTarget


class NoopPowerProvider(PowerProvider):
    provider_name = "noop"

    def wake(self, target: PowerTarget) -> PowerActionResult:
        del target
        return PowerActionResult(
            state="online",
            detail="Power provider is noop; assuming the execution plane is already powered.",
        )

    def status(self, target: PowerTarget) -> PowerActionResult:
        del target
        return PowerActionResult(
            state="online",
            detail="Power provider is noop; no out-of-band power status is available.",
        )
