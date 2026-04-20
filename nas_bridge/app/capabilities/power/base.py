from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class PowerTarget:
    name: str
    provider: str
    mac_address: str | None = None
    broadcast_ip: str | None = None
    metadata: dict[str, str] | None = None


@dataclass(slots=True)
class PowerActionResult:
    state: str
    detail: str | None = None


class PowerProvider(Protocol):
    provider_name: str

    def wake(self, target: PowerTarget) -> PowerActionResult:
        ...

    def status(self, target: PowerTarget) -> PowerActionResult:
        ...
