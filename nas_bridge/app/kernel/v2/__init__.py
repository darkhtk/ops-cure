"""Protocol v2 — Operation-centric, identity-first, event-sourced.

Five tables sit alongside the v1 chat_* / remote_* tables and do not
interfere with existing flow:

  - ``actors_v2``                  -- 1st-class identity entity
  - ``operations_v2``              -- unified Conversation+Task primitive
  - ``operation_participants_v2``  -- many-to-many actor<->op w/ role
  - ``operation_events_v2``        -- unified speech+lifecycle event log
  - ``operation_artifacts_v2``     -- multi-modal evidence references

The "V2" suffix on classes/tables is temporary -- it disambiguates
the SQLAlchemy registry while v1 OperationModel (the alias from PR8
remote_task promotion) still exists. When v1 is removed in F8 the
suffix gets dropped.

Phase status:
  F1 (this PR): schema + repository + tests, no dual-write yet.
  F2: Actor 1st-class wiring (token<->actor mapping).
  F3+: dual-write from v1 services so v2 catches up.
  F8: v1 removed; suffix dropped.
"""

from .models import (  # noqa: F401
    ActorV2Model,
    OperationV2Model,
    OperationParticipantV2Model,
    OperationEventV2Model,
    OperationArtifactV2Model,
)
from .repository import V2Repository  # noqa: F401
from . import contract  # noqa: F401  -- single-source vocab module
from .actor_service import ActorService, DEFAULT_OPERATOR_HANDLE  # noqa: F401
from .operation_mirror import OperationMirror  # noqa: F401
from .state_machine import (  # noqa: F401
    OperationStateMachine,
    StateMachineError,
    TransitionDecision,
    KIND_GENERAL,
    KIND_INQUIRY,
    KIND_PROPOSAL,
    KIND_TASK,
    STATE_OPEN,
    STATE_CLAIMED,
    STATE_EXECUTING,
    STATE_BLOCKED_APPROVAL,
    STATE_VERIFYING,
    STATE_CLOSED,
    ALLOWED_RESOLUTIONS,
    ALLOWED_TRANSITIONS,
)
from .capabilities import (  # noqa: F401
    CAP_CONVERSATION_OPEN,
    CAP_CONVERSATION_CLOSE,
    CAP_CONVERSATION_CLOSE_OPENER,
    CAP_CONVERSATION_HANDOFF,
    CAP_SPEECH_SUBMIT,
    CAP_TASK_CLAIM,
    CAP_TASK_COMPLETE,
    CAP_TASK_FAIL,
    CAP_TASK_APPROVE_DESTRUCTIVE,
    CapabilityService,
    make_capability_authorizer,
    make_per_capability_authorizer,
)

__all__ = [
    "ActorV2Model",
    "OperationV2Model",
    "OperationParticipantV2Model",
    "OperationEventV2Model",
    "OperationArtifactV2Model",
    "V2Repository",
    "ActorService",
    "DEFAULT_OPERATOR_HANDLE",
    "OperationMirror",
    "CapabilityService",
    "make_capability_authorizer",
    "make_per_capability_authorizer",
    "CAP_CONVERSATION_OPEN",
    "CAP_CONVERSATION_CLOSE",
    "CAP_CONVERSATION_CLOSE_OPENER",
    "CAP_CONVERSATION_HANDOFF",
    "CAP_SPEECH_SUBMIT",
    "CAP_TASK_CLAIM",
    "CAP_TASK_COMPLETE",
    "CAP_TASK_FAIL",
    "CAP_TASK_APPROVE_DESTRUCTIVE",
]
