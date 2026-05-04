"""v3 Policy Engine — enforces operation governance policy on writes.

Phase 1 stored ``operation.policy`` and event ``expected_response``
without enforcing them. Phase 2 (this module) enforces:

  * ``max_rounds``     — total speech-event count cap
  * ``kinds`` whitelist — replies whose kind isn't in the trigger
                          event's ``expected_response.kinds``
  * close policy        — ``opener_unilateral`` (legacy / default),
                          ``any_participant``, ``operator_ratifies``,
                          ``quorum``

Phase 2 deliberately does NOT touch:
  * by_round_seq expiry      (needs a background scheduler)
  * context_compaction       (needs an LLM caller, separate concern)
  * join_policy enforcement  (no JOIN speech act in phase 1; reuses
                              addressed_to auto-add semantics)

The engine is opt-in per op: ops with the default policy
(``close_policy=opener_unilateral`` and no ``max_rounds``) keep the
exact v2 behavior. Policies are validated at op-open time
(``conversation_service.open_conversation``); enforcement here is
applied only when the policy demands it.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import contract as _contract
from .models import OperationEventV2Model, OperationV2Model
from .repository import V2Repository


class PolicyViolation(ValueError):
    """Raised when a write would violate the op's governance policy.

    Carries a stable, machine-readable code so the API layer can map
    consistent HTTP responses (400 vs 403) without grepping the
    message string.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# Codes are stable wire contract — clients (agents, tests) match on these.
CODE_MAX_ROUNDS_EXHAUSTED = "policy.max_rounds_exhausted"
CODE_REPLY_KIND_REJECTED = "policy.reply_kind_rejected"
CODE_CLOSE_NEEDS_OPERATOR = "policy.close_needs_operator_ratify"
CODE_CLOSE_NEEDS_QUORUM = "policy.close_needs_quorum"
CODE_CLOSE_NEEDS_PARTICIPANT = "policy.close_needs_participant"
CODE_JOIN_INVITE_ONLY = "policy.join_invite_only"
CODE_INVITE_NEEDS_PARTICIPANT = "policy.invite_needs_participant"
CODE_CLOSE_NEEDS_ARTIFACT = "policy.close_needs_artifact"


class PolicyEngine:
    """Stateless gate. Caller passes a session + the op being written
    against; engine reads policy from the op's metadata and validates
    the proposed write."""

    def __init__(self, repo: V2Repository | None = None) -> None:
        self._repo = repo or V2Repository()

    # ---- speech writes ------------------------------------------------

    def check_speech_admissible(
        self,
        db: Session,
        *,
        op: OperationV2Model,
        actor_id: str,
        speech_kind: str,
        replies_to_event_id: str | None,
    ) -> None:
        """Raise ``PolicyViolation`` if appending this speech event
        would violate the op's policy. ``speech_kind`` is the trailing
        token (e.g. ``"claim"``, ``"object"``); the engine prepends
        ``chat.speech.`` when consulting the event log."""
        policy = self._repo.operation_policy(op)

        # max_rounds — count all speech-category events on this op,
        # regardless of transport prefix (phase 15). Lifecycle events
        # (conversation.opened/closed, task.*) don't count toward the
        # cap; only utterances do.
        max_rounds = policy.get("max_rounds")
        if max_rounds:
            existing = self._repo.count_speech_events(
                db, operation_id=op.id,
            )
            if existing >= max_rounds:
                raise PolicyViolation(
                    code=CODE_MAX_ROUNDS_EXHAUSTED,
                    detail=(
                        f"max_rounds={max_rounds} reached "
                        f"({existing} speech events already recorded)"
                    ),
                )

        # Reply-kind whitelist enforcement: when the trigger event
        # declared expected_response.kinds, the reply must satisfy it.
        #
        # Universal carve-outs (admissible regardless of whitelist):
        #
        #   - ``defer``    → "I cannot answer in the requested form".
        #                    Required for the auto-defer sweeper. Without
        #                    this, ``kinds=[answer]`` would block the
        #                    sweeper from emitting its by_round_seq fallback.
        #   - ``evidence`` → deliverable carrier (T1.2). A trigger
        #                    declaring ``kinds=[ratify,object]`` is asking
        #                    for a vote, NOT denying that the responder
        #                    can attach a patched file. Without this
        #                    carve-out, demand-patch loops deadlock:
        #                    [OBJECT kinds=agree,object] from a reviewer
        #                    blocks operator's [EVIDENCE re-post]. Spec
        #                    rev 8 / D1.
        #   - ``object``   → late-arriving counter-evidence is always
        #                    valid. Forbidding ``object`` would let a
        #                    poorly-narrowed whitelist convert a real
        #                    disagreement into silence. D5.
        #
        # Other kinds (claim, propose, ratify, agree, react, summarize,
        # block, invite, join, move_close, question, answer) remain
        # gated by the trigger's whitelist when set.
        _UNIVERSAL_KINDS = {"defer", "evidence", "object"}
        if speech_kind not in _UNIVERSAL_KINDS and replies_to_event_id:
            trigger = db.get(OperationEventV2Model, replies_to_event_id)
            if trigger is not None:
                ex = self._repo.event_expected_response(trigger)
                if ex is not None:
                    kinds = ex.get("kinds")
                    if kinds:
                        allowed = set(kinds)
                        if (
                            _contract.EXPECTED_RESPONSE_KIND_WILDCARD not in allowed
                            and speech_kind not in allowed
                        ):
                            raise PolicyViolation(
                                code=CODE_REPLY_KIND_REJECTED,
                                detail=(
                                    f"reply kind {speech_kind!r} not in "
                                    f"expected_response.kinds={sorted(allowed)} "
                                    f"declared by trigger event"
                                ),
                            )

    # ---- membership gates --------------------------------------------

    def check_join_admissible(
        self,
        db: Session,
        *,
        op: OperationV2Model,
        joiner_actor_id: str,
        is_invited: bool,
    ) -> None:
        """Enforce ``policy.join_policy`` on a ``speech.join`` event.

        - ``open``: anyone may join.
        - ``self_or_invite`` (default): the joiner may join freely.
          Equivalent to v2 behavior — kept as the default.
        - ``invite_only``: rejected unless the joiner is already
          listed in any participant role (typical: ``invited`` from a
          prior ``speech.invite``).
        """
        policy = self._repo.operation_policy(op)
        jp = policy.get("join_policy")
        if jp in (
            _contract.JOIN_POLICY_OPEN,
            _contract.JOIN_POLICY_SELF_OR_INVITE,
        ):
            return
        if jp == _contract.JOIN_POLICY_INVITE_ONLY:
            if is_invited:
                return
            raise PolicyViolation(
                code=CODE_JOIN_INVITE_ONLY,
                detail=(
                    "join_policy=invite_only requires a prior speech.invite "
                    "addressing this actor before they may join"
                ),
            )

    def check_invite_admissible(
        self,
        db: Session,
        *,
        op: OperationV2Model,
        inviter_actor_id: str,
    ) -> None:
        """Only existing participants may invite. This is the
        symmetric guard that makes ``invite_only`` join policy
        meaningful — otherwise an outsider could invite themselves."""
        participants = self._repo.list_participants(db, operation_id=op.id)
        if any(p.actor_id == inviter_actor_id for p in participants):
            return
        raise PolicyViolation(
            code=CODE_INVITE_NEEDS_PARTICIPANT,
            detail="speech.invite must come from an existing participant",
        )

    # ---- close gate ---------------------------------------------------

    def check_close_admissible(
        self,
        db: Session,
        *,
        op: OperationV2Model,
        closer_actor_id: str | None,
        resolution: str | None = None,
    ) -> None:
        """Raise ``PolicyViolation`` if the op's close policy is not
        satisfied. Engine assumes the caller already passed the basic
        capability check (``CAP_CONVERSATION_CLOSE`` / ``_OPENER``);
        this layer adds the policy-derived requirements on top.
        """
        policy = self._repo.operation_policy(op)
        cp = policy.get("close_policy")

        # T2.1 — orthogonal to close_policy. Even if the close_policy
        # vote/quorum/role check passes, requires_artifact gates the
        # close on having ≥1 OperationArtifact attached. Useful for
        # kind=task / kind=proposal where deliverable existence is a
        # completion criterion.
        #
        # P10.4 — gate ONLY applies to terminal-success resolutions.
        # An abandoned / cancelled / failed close is by definition
        # admitting no deliverable was produced; demanding an
        # artifact would force callers to fabricate one (Unity
        # arcade smoke 2026-05-04: alice attached a build that
        # nobody actually verified just to satisfy the gate). The
        # success vocabulary (per kind) lives in
        # contract.ALLOWED_RESOLUTIONS but at the gate-level we
        # use a simple "is it a non-failure resolution?" check.
        _NON_SUCCESS_RESOLUTIONS = {
            "abandoned", "cancelled", "failed", "withdrawn",
            "superseded", "dropped",
        }
        is_terminal_success = (
            resolution is None
            or resolution not in _NON_SUCCESS_RESOLUTIONS
        )
        if policy.get("requires_artifact") and is_terminal_success:
            artifacts = self._repo.list_artifacts_for_operation(
                db, operation_id=op.id,
            )
            if not artifacts:
                raise PolicyViolation(
                    code=CODE_CLOSE_NEEDS_ARTIFACT,
                    detail=(
                        "policy.requires_artifact=true but no "
                        "OperationArtifact is attached to this op; "
                        "post a speech.evidence with payload.artifact "
                        "before closing (or use a non-success "
                        "resolution like 'abandoned'/'cancelled'/"
                        "'failed' — the artifact gate is bypassed "
                        "for those terminal-failure paths)"
                    ),
                )

        if cp == _contract.CLOSE_POLICY_OPENER_UNILATERAL:
            return  # legacy / default — capability already gated

        if cp == _contract.CLOSE_POLICY_ANY_PARTICIPANT:
            if closer_actor_id is None:
                return  # system-driven close (idle sweep, etc.) — allowed
            participants = self._repo.list_participants(db, operation_id=op.id)
            if not any(p.actor_id == closer_actor_id for p in participants):
                raise PolicyViolation(
                    code=CODE_CLOSE_NEEDS_PARTICIPANT,
                    detail="any_participant close requires the closer to be a participant",
                )
            return

        if cp == _contract.CLOSE_POLICY_OPERATOR_RATIFIES:
            self._require_role_ratify(db, op=op, role="operator")
            return

        if cp == _contract.CLOSE_POLICY_QUORUM:
            min_r = int(policy.get("min_ratifiers") or 0)
            self._require_quorum_ratify(db, op=op, min_ratifiers=min_r)
            return

        # Unknown policy → fail loud rather than silently bypass.
        raise PolicyViolation(
            code=CODE_CLOSE_NEEDS_PARTICIPANT,
            detail=f"unknown close_policy={cp!r}",
        )

    # ---- internal helpers --------------------------------------------

    def _ratify_events_by_actor(
        self, db: Session, *, op: OperationV2Model
    ) -> dict[str, OperationEventV2Model]:
        """Map of actor_id -> their most recent CLOSE-INTENT
        ``chat.speech.ratify`` event on this op. De-duped so the same
        actor double-ratifying doesn't inflate quorum counts.

        D9 (rev 9) — `ratify` is overloaded: agents use it both to
        agree with a spec proposal AND to vote for closing the op.
        Quorum should count ONLY the latter, otherwise spec
        consensus on early proposals trips a premature close
        attempt that gets blocked by ``requires_artifact`` and
        burns retry cycles. (Observed in Unity arcade smoke
        2026-05-04: 3 ratifiers within 64s on a spec proposal,
        op stalled for an hour because no one then built anything.)

        A ratify event is treated as close-intent when ANY hold:

          - ``payload.intent == "close"`` (explicit, recommended)
          - ``replies_to_event_id`` points at a ``chat.speech.move_close``
          - ``replies_to_event_id`` points at an event that has at
            least one attached ``OperationArtifact`` (i.e., the
            ratifier is endorsing a deliverable)

        Spec ratifies (no replies_to / replies to a plain propose)
        are still recorded — they just don't count toward quorum.
        Reference clients are encouraged to send
        ``payload.intent="close"`` explicitly; the heuristic above
        is the back-compat fallback.
        """
        events = self._repo.list_events(
            db,
            operation_id=op.id,
            limit=1000,
        )
        latest: dict[str, OperationEventV2Model] = {}
        for ev in events:
            # Phase 15: match speech-category ratify regardless of
            # transport prefix (chat.speech.ratify, cli.speech.ratify,
            # speech.ratify all qualify).
            if _contract.speech_action(ev.kind) != "ratify":
                continue
            if not self._is_close_intent_ratify(db, ev):
                continue
            latest[ev.actor_id] = ev
        return latest

    def _is_close_intent_ratify(
        self, db: Session, ev: OperationEventV2Model,
    ) -> bool:
        """D9 helper: classify a ratify event as voting for close.

        Checked in order:
          1) explicit ``payload.intent == "close"``
          2) ``replies_to_event_id`` → ``chat.speech.move_close``
          3) ``replies_to_event_id`` → event has an attached artifact
          4) op has ANY artifact attached AT OR BEFORE this ratify's
             seq (back-compat heuristic: a ratify that lands after
             evidence-with-artifact has been delivered is presumed
             to be voting on the deliverable, not on a stale spec)

        Otherwise: spec ratify (recorded but not counted).
        """
        # 1) explicit close-intent flag
        try:
            payload = self._repo.event_payload(ev) or {}
            if isinstance(payload, dict):
                intent = payload.get("intent")
                if isinstance(intent, str) and intent.lower() == "close":
                    return True
        except Exception:  # noqa: BLE001
            pass

        # 2) + 3) trigger-based signals
        if ev.replies_to_event_id:
            trigger = db.get(OperationEventV2Model, ev.replies_to_event_id)
            if trigger is not None:
                if _contract.speech_action(trigger.kind) == "move_close":
                    return True
                try:
                    if self._repo.list_artifacts_for_event(db, event_id=trigger.id):
                        return True
                except Exception:  # noqa: BLE001
                    pass

        # 4) back-compat heuristic: any artifact already attached to
        #    this op before the ratify event was written
        try:
            arts = self._repo.list_artifacts_for_operation(
                db, operation_id=ev.operation_id,
            )
            for a in arts:
                # If the artifact's source event seq is ≤ the ratify
                # seq, the ratifier had visibility on the deliverable.
                src = db.get(OperationEventV2Model, a.event_id)
                if src is not None and src.seq <= ev.seq:
                    return True
        except Exception:  # noqa: BLE001
            pass

        return False

    def _require_role_ratify(
        self, db: Session, *, op: OperationV2Model, role: str,
    ) -> None:
        participants = self._repo.list_participants(db, operation_id=op.id)
        role_actor_ids = {p.actor_id for p in participants if p.role == role}
        if not role_actor_ids:
            raise PolicyViolation(
                code=CODE_CLOSE_NEEDS_OPERATOR,
                detail=(
                    f"close_policy=operator_ratifies requires at least one "
                    f"participant with role={role!r} on the op"
                ),
            )
        ratifies = self._ratify_events_by_actor(db, op=op)
        if not (role_actor_ids & set(ratifies.keys())):
            raise PolicyViolation(
                code=CODE_CLOSE_NEEDS_OPERATOR,
                detail=(
                    f"close_policy=operator_ratifies requires a "
                    f"chat.speech.ratify event from a participant with "
                    f"role={role!r}"
                ),
            )

    def _require_quorum_ratify(
        self, db: Session, *, op: OperationV2Model, min_ratifiers: int,
    ) -> None:
        if min_ratifiers <= 0:
            return
        ratifies = self._ratify_events_by_actor(db, op=op)
        if len(ratifies) < min_ratifiers:
            raise PolicyViolation(
                code=CODE_CLOSE_NEEDS_QUORUM,
                detail=(
                    f"close_policy=quorum requires {min_ratifiers} distinct "
                    f"ratifiers, only {len(ratifies)} have ratified"
                ),
            )
