from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class ExecutionTarget:
    name: str
    provider: str
    platform: str
    launcher_id_hint: str | None = None
    host_pattern: str | None = None
    auto_start_expected: bool = True
    metadata: dict[str, str] | None = None


@dataclass(slots=True)
class ExecutionStatus:
    state: str
    launcher_id: str | None = None
    detail: str | None = None


class ExecutionProvider(Protocol):
    provider_name: str

    def status_for_project(self, *, project_name: str, target: ExecutionTarget) -> ExecutionStatus:
        ...

    def request_start(self, *, project_name: str, target: ExecutionTarget) -> ExecutionStatus:
        ...
