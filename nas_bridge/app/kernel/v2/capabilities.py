"""F9: capability-based authorization on v2 Actors.

Replaces the loose ``actor_authorizer`` callback (string-match against
caller_context) with explicit capability strings stored on the v2
Actor row. ``CapabilityService.actor_can(handle, capability)`` is the
single decision point.

Capabilities follow ``noun.verb[:scope]`` and are deliberately simple:

  conversation.open
  conversation.close                # unrestricted close (admin/system)
  conversation.close.opener         # owner-or-opener-only close
  conversation.handoff
  speech.submit
  task.claim
  task.complete
  task.fail
  task.approve.destructive          # gates destructive evidence

Default capability sets (used when the Actor row was auto-provisioned
by ActorService and nobody set capabilities explicitly):

  human kind: ALL_DEFAULT_HUMAN
    -- everything but task.approve.destructive (operators escalate
       explicitly)
  ai kind:    ALL_DEFAULT_AI
    -- talk + claim + evidence; cannot close other people's
       conversations or approve destructive ops without explicit grant
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy.orm import Session

from .actor_service import ActorService
from .repository import V2Repository

CAP_CONVERSATION_OPEN = "conversation.open"
CAP_CONVERSATION_CLOSE = "conversation.close"
CAP_CONVERSATION_CLOSE_OPENER = "conversation.close.opener"
CAP_CONVERSATION_HANDOFF = "conversation.handoff"
CAP_SPEECH_SUBMIT = "speech.submit"
CAP_TASK_CLAIM = "task.claim"
CAP_TASK_COMPLETE = "task.complete"
CAP_TASK_FAIL = "task.fail"
CAP_TASK_APPROVE_DESTRUCTIVE = "task.approve.destructive"


ALL_DEFAULT_HUMAN: tuple[str, ...] = (
    CAP_CONVERSATION_OPEN,
    CAP_CONVERSATION_CLOSE,
    CAP_CONVERSATION_CLOSE_OPENER,
    CAP_CONVERSATION_HANDOFF,
    CAP_SPEECH_SUBMIT,
    CAP_TASK_CLAIM,
    CAP_TASK_COMPLETE,
    CAP_TASK_FAIL,
)

ALL_DEFAULT_AI: tuple[str, ...] = (
    CAP_CONVERSATION_OPEN,
    CAP_CONVERSATION_CLOSE_OPENER,
    CAP_CONVERSATION_HANDOFF,
    CAP_SPEECH_SUBMIT,
    CAP_TASK_CLAIM,
    CAP_TASK_COMPLETE,
    CAP_TASK_FAIL,
)


def _normalize_handle(value: str) -> str:
    return value if value.startswith("@") else f"@{value}"


class CapabilityService:
    def __init__(
        self,
        repo: V2Repository | None = None,
        actor_service: ActorService | None = None,
    ) -> None:
        self._repo = repo or V2Repository()
        self._actors = actor_service or ActorService(self._repo)

    def actor_can(
        self,
        db: Session,
        *,
        actor_handle: str,
        capability: str,
    ) -> bool:
        handle = _normalize_handle(actor_handle)
        actor = self._repo.get_actor_by_handle(db, handle)
        if actor is None:
            return False
        caps = self._effective_capabilities(actor)
        return capability in caps

    def grant(
        self,
        db: Session,
        *,
        actor_handle: str,
        capabilities: Iterable[str],
    ) -> list[str]:
        """Add capabilities to an actor; returns the new full list.
        ensure_actor_by_handle bootstraps the row if absent."""
        actor = self._actors.ensure_actor_by_handle(
            db, handle=_normalize_handle(actor_handle),
        )
        existing = list(self._repo.actor_capabilities(actor))
        added = [c for c in capabilities if c not in existing]
        if not added:
            return existing
        new_list = existing + added
        # Re-store via repo's _dumps -- piggy-back on insert path using
        # raw column update for speed.
        actor.capabilities_json = _serialize(new_list)
        db.flush()
        return new_list

    def revoke(
        self,
        db: Session,
        *,
        actor_handle: str,
        capabilities: Iterable[str],
    ) -> list[str]:
        actor = self._repo.get_actor_by_handle(
            db, _normalize_handle(actor_handle),
        )
        if actor is None:
            return []
        existing = list(self._repo.actor_capabilities(actor))
        revoke_set = set(capabilities)
        kept = [c for c in existing if c not in revoke_set]
        if len(kept) == len(existing):
            return existing
        actor.capabilities_json = _serialize(kept)
        db.flush()
        return kept

    def _effective_capabilities(self, actor) -> set[str]:
        explicit = list(self._repo.actor_capabilities(actor))
        if explicit:
            return set(explicit)
        # No explicit capabilities yet -- fall back to kind defaults so
        # the system stays usable while operators are still wiring
        # explicit grants. Once anything has been set explicitly the
        # defaults stop applying (an operator who revoked everything
        # really meant it).
        if actor.kind == "human":
            return set(ALL_DEFAULT_HUMAN)
        return set(ALL_DEFAULT_AI)


def _serialize(values: list[str]) -> str:
    import json
    return json.dumps(values, ensure_ascii=False)


def make_capability_authorizer(
    capability_service: CapabilityService,
    *,
    capability: str,
    auto_provision: bool = True,
):
    """Adapter so ChatConversationService.actor_authorizer keeps its
    (caller_context, actor_name) -> bool shape while delegating to
    capability checks. ``caller_context`` is currently unused at this
    seam -- the asserted_client_id is already baked into actor_name
    via the upstream service layer.

    G1: ``auto_provision=True`` (default) auto-creates the actor row
    when it doesn't exist yet, so the first speech / open call from a
    fresh handle gets the kind-default capability set instead of an
    immediate denial. The mirror_conversation_open path would create
    the same row a moment later anyway -- this just eliminates a
    chicken-and-egg with _check_actor running before the mirror.
    """
    from ...db import session_scope

    def _authorize(caller_context, actor_name: str) -> bool:  # noqa: ARG001
        with session_scope() as db:
            if auto_provision:
                actor_service = capability_service._actors  # noqa: SLF001
                actor_service.ensure_actor_by_handle(
                    db,
                    handle=actor_name if actor_name.startswith("@") else f"@{actor_name}",
                )
            return capability_service.actor_can(
                db,
                actor_handle=actor_name,
                capability=capability,
            )

    return _authorize
