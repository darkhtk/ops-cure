"""Behavior registry for generic plugin-style wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .chat.binding import build_chat_discord_binding
from .chat.kernel_binding import build_chat_kernel_binding
from .chat.service import ChatBehaviorService
from .orchestration.binding import build_orchestration_discord_binding
from .orchestration.kernel_binding import build_orchestration_kernel_binding
from .orchestration.policy import PolicyService
from .orchestration.recovery import RecoveryService
from .orchestration.service import SessionService
from .orchestration.verification import VerificationService
from .ops.binding import build_ops_discord_binding
from .ops.kernel_binding import build_ops_kernel_binding
from .ops.service import OpsBehaviorService
from ..kernel.bindings import KernelBehaviorBinding
from ..kernel.registry import WorkerRegistry
from ..transports.discord.bindings import DiscordBehaviorBinding
from ..transports.discord.threads import ThreadManager


@dataclass(frozen=True, slots=True)
class BehaviorContext:
    registry: WorkerRegistry
    thread_manager: ThreadManager
    chat_service: ChatBehaviorService
    ops_service: OpsBehaviorService
    policy_service: PolicyService
    recovery_service: RecoveryService
    session_service: SessionService
    verification_service: VerificationService


class BehaviorDescriptor(Protocol):
    behavior_id: str
    display_name: str
    description: str

    def build_discord_binding(self, context: BehaviorContext) -> DiscordBehaviorBinding | None:
        ...

    def build_kernel_binding(self, context: BehaviorContext) -> KernelBehaviorBinding | None:
        ...


@dataclass(frozen=True, slots=True)
class OrchestrationBehaviorDescriptor:
    behavior_id: str = "orchestration"
    display_name: str = "Orchestration"
    description: str = "Discord-native project orchestration behavior backed by the legacy workflow engine."

    def build_discord_binding(self, context: BehaviorContext) -> DiscordBehaviorBinding | None:
        return build_orchestration_discord_binding(
            session_service=context.session_service,
            verification_service=context.verification_service,
            registry=context.registry,
        )

    def build_kernel_binding(self, context: BehaviorContext) -> KernelBehaviorBinding | None:
        return build_orchestration_kernel_binding()


@dataclass(frozen=True, slots=True)
class ChatBehaviorDescriptor:
    behavior_id: str = "chat"
    display_name: str = "Chat"
    description: str = "Cross-PC Codex dialogue behavior without task or handoff semantics."

    def build_discord_binding(self, context: BehaviorContext) -> DiscordBehaviorBinding | None:
        return build_chat_discord_binding(
            chat_service=context.chat_service,
            thread_manager=context.thread_manager,
        )

    def build_kernel_binding(self, context: BehaviorContext) -> KernelBehaviorBinding | None:
        return build_chat_kernel_binding()


@dataclass(frozen=True, slots=True)
class OpsBehaviorDescriptor:
    behavior_id: str = "ops"
    display_name: str = "Ops"
    description: str = "Lightweight operations and incident room behavior."

    def build_discord_binding(self, context: BehaviorContext) -> DiscordBehaviorBinding | None:
        return build_ops_discord_binding(
            ops_service=context.ops_service,
            thread_manager=context.thread_manager,
        )

    def build_kernel_binding(self, context: BehaviorContext) -> KernelBehaviorBinding | None:
        return build_ops_kernel_binding()


@dataclass(frozen=True, slots=True)
class RemoteCodexBehaviorDescriptor:
    behavior_id: str = "remote_codex"
    display_name: str = "Remote Codex"
    description: str = (
        "Browser-first remote Codex behavior scaffold. "
        "Canonical task/evidence truth is migrating here, but live bindings are not enabled yet."
    )

    def build_discord_binding(self, context: BehaviorContext) -> DiscordBehaviorBinding | None:
        del context
        return None

    def build_kernel_binding(self, context: BehaviorContext) -> KernelBehaviorBinding | None:
        del context
        from .remote_codex.kernel_binding import build_remote_codex_kernel_binding

        return build_remote_codex_kernel_binding()


def default_behavior_descriptors() -> tuple[BehaviorDescriptor, ...]:
    return (
        OrchestrationBehaviorDescriptor(),
        ChatBehaviorDescriptor(),
        OpsBehaviorDescriptor(),
        RemoteCodexBehaviorDescriptor(),
    )


def resolve_discord_bindings(
    *,
    context: BehaviorContext,
    descriptors: tuple[BehaviorDescriptor, ...] | list[BehaviorDescriptor],
) -> list[DiscordBehaviorBinding]:
    bindings: list[DiscordBehaviorBinding] = []
    for descriptor in descriptors:
        binding = descriptor.build_discord_binding(context)
        if binding is not None:
            bindings.append(binding)
    return bindings


def resolve_kernel_bindings(
    *,
    context: BehaviorContext,
    descriptors: tuple[BehaviorDescriptor, ...] | list[BehaviorDescriptor],
) -> list[KernelBehaviorBinding]:
    bindings: list[KernelBehaviorBinding] = []
    for descriptor in descriptors:
        binding = descriptor.build_kernel_binding(context)
        if binding is not None:
            bindings.append(binding)
    return bindings
