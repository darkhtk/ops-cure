"""F7: v2 read endpoints — operation summary, event log, artifacts.

Reads come exclusively from operation_*_v2 tables. Privacy is enforced
here: events with ``private_to_actor_ids`` are filtered out unless
the requesting actor (taken from ``actor_handle`` query param) is in
the list. v1 routes remain in place but are now strictly authoritative
for fields v2 doesn't carry yet (e.g. v1-specific resolution vocab
strings round-trip exactly).
"""
from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request,
)
from pydantic import BaseModel, Field

from ..auth import (
    BridgeCaller, TOKEN_SCOPE_SPEAK,
    require_bridge_caller, require_scope, verify_actor_handle_claim,
)
from ..db import session_scope
from ..kernel.v2 import V2Repository
from ..kernel.v2.models import OperationEventV2Model

router = APIRouter(prefix="/v2/operations", tags=["v2-operations", "protocol-v3-public"])


def _normalize_handle(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith("@") else f"@{value}"


def _serialize_event(ev, repo: V2Repository) -> dict[str, Any]:
    return {
        "id": ev.id,
        "operation_id": ev.operation_id,
        "actor_id": ev.actor_id,
        "seq": ev.seq,
        "kind": ev.kind,
        "payload": repo.event_payload(ev),
        "addressed_to_actor_ids": repo.event_addressed_to(ev),
        "private_to_actor_ids": repo.event_private_to(ev),
        "replies_to_event_id": ev.replies_to_event_id,
        # v3-additive: explicit reply contract.
        "expected_response": repo.event_expected_response(ev),
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


def _serialize_operation(op, repo: V2Repository) -> dict[str, Any]:
    return {
        "id": op.id,
        "space_id": op.space_id,
        "kind": op.kind,
        "state": op.state,
        "title": op.title,
        "intent": op.intent,
        "metadata": repo.operation_metadata(op),
        # v3-additive: surface the normalized governance policy
        # explicitly so callers don't have to dig into metadata.
        "policy": repo.operation_policy(op),
        "resolution": op.resolution,
        "resolution_summary": op.resolution_summary,
        "closed_by_actor_id": op.closed_by_actor_id,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
        "closed_at": op.closed_at.isoformat() if op.closed_at else None,
    }


def _resolve_actor_id(repo: V2Repository, db, actor_handle: str | None) -> str | None:
    handle = _normalize_handle(actor_handle)
    if not handle:
        return None
    actor = repo.get_actor_by_handle(db, handle)
    return actor.id if actor else None


@router.get("/{operation_id}")
def get_operation(
    operation_id: str,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        body = _serialize_operation(op, repo)
        body["participants"] = [
            {"actor_id": p.actor_id, "role": p.role, "last_seen_seq": p.last_seen_seq}
            for p in repo.list_participants(db, operation_id=op.id)
        ]
        return body


@router.get("/{operation_id}/events")
def list_events(
    operation_id: str,
    after_seq: int | None = Query(default=None, ge=0),
    kinds: str | None = Query(default=None, description="Comma-separated kinds"),
    actor_handle: str | None = Query(
        default=None,
        description="Requesting actor's handle for privacy filtering (whisper redaction).",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    kind_filter: list[str] | None = None
    if kinds:
        kind_filter = [k.strip() for k in kinds.split(",") if k.strip()]
    repo = V2Repository()
    items: list[dict[str, Any]] = []
    redacted_count = 0
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        viewer_id = _resolve_actor_id(repo, db, actor_handle)
        events = repo.list_events(
            db,
            operation_id=operation_id,
            after_seq=after_seq,
            kinds=kind_filter,
            limit=limit,
        )
        for ev in events:
            private_to = repo.event_private_to(ev)
            if private_to is not None:
                # whisper -- only the listed actors + the speaker may see
                if viewer_id is None or (viewer_id != ev.actor_id and viewer_id not in private_to):
                    redacted_count += 1
                    continue
            items.append(_serialize_event(ev, repo))
    return {
        "operation_id": operation_id,
        "events": items,
        "redacted_count": redacted_count,
        "viewer_actor_id": viewer_id,
    }


@router.get("/{operation_id}/artifacts")
def list_artifacts(
    operation_id: str,
    kind: str | None = Query(default=None),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        rows = repo.list_artifacts_for_operation(db, operation_id=operation_id, kind=kind)
        return {
            "operation_id": operation_id,
            "artifacts": [
                {
                    "id": a.id,
                    "event_id": a.event_id,
                    "kind": a.kind,
                    "uri": a.uri,
                    "sha256": a.sha256,
                    "mime": a.mime,
                    "size_bytes": a.size_bytes,
                    "label": a.label,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in rows
            ],
        }


@router.post("/{operation_id}/seen")
def mark_seen(
    operation_id: str,
    actor_handle: str = Query(...),
    seq: int = Query(..., ge=0),
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """Advance the actor's last_seen_seq cursor on this operation.
    Idempotent and monotonic -- if seq is below the current cursor it
    is ignored. Drives unread badge counts."""
    repo = V2Repository()
    handle = _normalize_handle(actor_handle)
    with session_scope() as db:
        actor = repo.get_actor_by_handle(db, handle) if handle else None
        if actor is None:
            raise HTTPException(status_code=404, detail=f"actor {actor_handle!r} not found")
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        repo.update_participant_seen_seq(
            db, operation_id=operation_id, actor_id=actor.id, seq=seq,
        )
        return {"operation_id": operation_id, "actor_id": actor.id, "last_seen_seq": seq}


# G2: native v2 write endpoints. Internally these still delegate to
# ChatConversationService so v1 dual-write keeps producing v1 rows;
# clients no longer need to know about /api/chat or v1_conversation_id.
# Once F8's hard removal lands, the delegation collapses to v2-only
# writes.


def _strip_at(value: str) -> str:
    return value[1:] if value.startswith("@") else value


def _operation_to_v1_conversation_id(operation_id: str) -> str:
    """Look up the v1 conversation id linked to a v2 operation. Until
    F8 removes v1, we use this to route writes through the existing
    chat service."""
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail=f"operation {operation_id} not found")
        meta = repo.operation_metadata(op)
        v1_id = meta.get("v1_conversation_id")
        if not v1_id:
            raise HTTPException(
                status_code=409,
                detail=f"operation {operation_id} has no v1 mirror; cannot write",
            )
        return str(v1_id)


class V2OpenOperationRequest(BaseModel):
    """v2-flavored open. ``space_id`` carries the routing target; in the
    chat-only era this is the discord_thread_id (the bridge resolves
    it via chat_threads.discord_thread_id). When non-chat spaces land,
    ``space_kind`` becomes a discriminator."""
    space_id: str = Field(..., description="discord thread id (chat-only era)")
    kind: str
    title: str
    intent: str | None = None
    addressed_to: str | None = None
    opener_actor_handle: str
    objective: str | None = None
    success_criteria: dict[str, Any] | None = None
    # v3-additive op governance policy. See
    # kernel.v2.contract.DEFAULT_OPERATION_POLICY for the shape +
    # defaults. Stored under op.metadata.policy on dual-write.
    policy: dict[str, Any] | None = None


class V2EventRequest(BaseModel):
    actor_handle: str
    kind: str = Field(..., description="speech.claim / speech.question / ...")
    payload: dict[str, Any] = Field(default_factory=dict)
    addressed_to: str | None = None
    addressed_to_many: list[str] | None = None
    replies_to_event_id: str | None = None
    private_to_actors: list[str] | None = None
    # v3-additive: declare who is expected to respond + how. See
    # kernel.v2.contract.validate_expected_response.
    expected_response: dict[str, Any] | None = None


class V2CloseRequest(BaseModel):
    actor_handle: str
    resolution: str
    summary: str | None = None


# H3: native v2 task lifecycle endpoints. Each delegates to
# ChatTaskCoordinator (still v1-authoritative for lease state) but
# the SDK callers no longer need /api/chat or v1 conversation ids.
class V2ClaimRequest(BaseModel):
    actor_handle: str
    lease_seconds: int = 300


class V2EvidenceRequest(BaseModel):
    actor_handle: str
    lease_token: str
    kind: str  # EvidenceKind from contract
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


class V2ApprovalRequestRequest(BaseModel):
    actor_handle: str
    lease_token: str
    reason: str
    note: str | None = None


class V2ApprovalResolveRequest(BaseModel):
    actor_handle: str
    resolution: str  # 'approved' | 'denied'
    note: str | None = None


class V2CompleteRequest(BaseModel):
    actor_handle: str
    lease_token: str
    summary: str | None = None


class V2FailRequest(BaseModel):
    actor_handle: str
    lease_token: str
    error_text: str


@router.post("", status_code=201)
def open_operation(
    payload: V2OpenOperationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
    x_actor_token: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_actor_handle_claim(
        request,
        claimed_handle=payload.opener_actor_handle,
        x_actor_token=x_actor_token,
    )
    require_scope(request, x_actor_token=x_actor_token, needed=TOKEN_SCOPE_SPEAK)
    """Open a new operation. Delegates to ChatConversationService.open_conversation
    in the chat-only era; the response surfaces the v2 operation id
    directly so callers never need the v1 conversation id."""
    services = request.app.state.services
    chat_service = services.chat_conversation_service
    # Lazy import keeps API module light at startup.
    from ..behaviors.chat.conversation_schemas import ConversationOpenRequest
    from ..behaviors.chat.conversation_service import (
        ChatThreadNotFoundError, ChatConversationStateError,
    )
    # Pre-create the thread's general conversation in its own session.
    # If we don't, kind=task triggers create_task inside open_conversation's
    # session, which on SQLite locks because the outer session has
    # already written the (newly created) general row. Calling
    # ensure_general first commits the general row separately.
    try:
        chat_service.ensure_general(discord_thread_id=payload.space_id)
    except ChatThreadNotFoundError:
        raise HTTPException(status_code=404, detail=f"space {payload.space_id} not found")
    open_kwargs: dict[str, Any] = {
        "kind": payload.kind,
        "title": payload.title,
        "intent": payload.intent,
        "addressed_to": payload.addressed_to,
        "opener_actor": _strip_at(payload.opener_actor_handle),
        "objective": payload.objective,
    }
    if payload.success_criteria is not None:
        open_kwargs["success_criteria"] = payload.success_criteria
    # v3-flavored default: collab-task ops opened via /v2/operations do
    # not bind a RemoteTask unless the caller explicitly requests one.
    # The v1 chat path (no /v2 wrapper) keeps the legacy default
    # (bind=True) so existing v1 chat tests stay green. Callers wanting
    # actual executor lifecycle should send
    # ``policy.bind_remote_task: true`` explicitly.
    effective_policy: dict[str, Any] = dict(payload.policy or {})
    if payload.kind == "task" and "bind_remote_task" not in effective_policy:
        effective_policy["bind_remote_task"] = False
    if effective_policy:
        open_kwargs["policy"] = effective_policy
    try:
        summary = chat_service.open_conversation(
            discord_thread_id=payload.space_id,
            request=ConversationOpenRequest(**open_kwargs),
        )
    except ChatThreadNotFoundError:
        raise HTTPException(status_code=404, detail=f"space {payload.space_id} not found")
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    repo = V2Repository()
    with session_scope() as db:
        # summary.id is the v1 conversation id; resolve to v2 op id.
        from ..behaviors.chat.models import ChatConversationModel
        v1 = db.get(ChatConversationModel, summary.id)
        if v1 is None or v1.v2_operation_id is None:
            raise HTTPException(
                status_code=500,
                detail="open succeeded but v2 mirror missing -- check dual-write",
            )
        op = repo.get_operation(db, v1.v2_operation_id)
        # P9.5 — forward an op-opened marker to the parent Discord
        # thread so operators see the lifecycle, not just the
        # speech inside it. Best-effort.
        from ..behaviors.chat.models import ChatThreadModel
        thread_row = db.get(ChatThreadModel, v1.thread_id)
        discord_thread_id = thread_row.discord_thread_id if thread_row else None
        if discord_thread_id:
            policy_summary = repo.operation_policy(op) or {}
            short = (
                f"close={policy_summary.get('close_policy','?')}"
                + (f"/quorum={policy_summary.get('min_ratifiers')}"
                   if policy_summary.get('close_policy') == 'quorum' else "")
                + (" /req-artifact" if policy_summary.get('requires_artifact') else "")
            )
            forwarded = (
                f"📣 **op opened** _{op.kind}_ — `{op.title}`\n"
                f"opener: {payload.opener_actor_handle} · policy: {short}\n"
                f"id: `{op.id[:8]}…`"
            )
            background_tasks.add_task(
                _post_to_discord_safely,
                services.thread_manager, discord_thread_id, forwarded,
            )
        return _serialize_operation(op, repo) | {"id": op.id}


@router.post("/{operation_id}/events", status_code=201)
def append_event(
    operation_id: str,
    payload: V2EventRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
    x_actor_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Append a speech / lifecycle event to an operation. Currently the
    only kinds wired through this endpoint are speech.* (claim, question,
    proposal, etc.). Task-lifecycle and approval flows still go through
    /api/chat/conversations/.../task; G2 deliberately does not move
    those because the lease-token contract is non-trivial."""
    # v3 phase 3.x identity check: if X-Actor-Token is present, the
    # claimed actor_handle must match the bound actor. When
    # BRIDGE_REQUIRE_ACTOR_TOKEN=1 the header is mandatory; otherwise
    # legacy mode (shared bearer + asserted handle) is permitted.
    verify_actor_handle_claim(
        request, claimed_handle=payload.actor_handle, x_actor_token=x_actor_token,
    )
    require_scope(request, x_actor_token=x_actor_token, needed=TOKEN_SCOPE_SPEAK)
    if not payload.kind.startswith("speech."):
        raise HTTPException(
            status_code=400,
            detail=(
                f"event kind {payload.kind!r} cannot be appended via this endpoint; "
                "only speech.* kinds are supported in G2 (task lifecycle stays "
                "on /api/chat for now)."
            ),
        )
    speech_kind = payload.kind.split(".", 1)[1]
    v1_id = _operation_to_v1_conversation_id(operation_id)
    services = request.app.state.services
    from ..behaviors.chat.conversation_schemas import SpeechActSubmitRequest
    from ..behaviors.chat.conversation_service import (
        ChatConversationNotFoundError, ChatConversationStateError, ChatActorIdentityError,
    )
    text = str(payload.payload.get("text", ""))
    if not text:
        raise HTTPException(
            status_code=400,
            detail="payload.text is required for speech.* events",
        )
    # P9.7 / D3 — warn (best-effort) when expected_response invites
    # handles that don't resolve to existing actors. Reviewers
    # have been seen inventing handles like @autoplayer1 / @auditor
    # which silently waste obligation slots and pollute routing.
    # Soft-only: log + ignore. Strict mode (reject 400) gated by
    # ``BRIDGE_REQUIRE_KNOWN_HANDLES=1``.
    ex = payload.expected_response or {}
    inv_handles = ex.get("from_actor_handles") or []
    if inv_handles:
        from ..kernel.v2 import V2Repository as _V2Repo
        _vrepo = _V2Repo()
        unknown: list[str] = []
        with session_scope() as _db:
            for h in inv_handles:
                norm = h if str(h).startswith("@") else f"@{h}"
                if _vrepo.get_actor_by_handle(_db, norm) is None:
                    unknown.append(norm)
        if unknown:
            import os as _os
            import logging as _logging
            strict = _os.environ.get(
                "BRIDGE_REQUIRE_KNOWN_HANDLES", ""
            ).strip().lower() in {"1", "true", "yes", "on"}
            if strict:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"expected_response.from_actor_handles contains "
                        f"unknown actor(s): {unknown}"
                    ),
                )
            _logging.getLogger(__name__).warning(
                "expected_response invites unknown actor handles "
                "%s on op=%s; obligation slot(s) will not match any "
                "registered actor", unknown, operation_id,
            )
    try:
        speech = services.chat_conversation_service.submit_speech(
            conversation_id=v1_id,
            request=SpeechActSubmitRequest(
                actor_name=_strip_at(payload.actor_handle),
                kind=speech_kind,
                content=text,
                addressed_to=payload.addressed_to,
                addressed_to_many=payload.addressed_to_many or [],
                replies_to_speech_id=None,  # v2 callers use v2 ids
                # v3-additive: pass v2 reply id straight through so the
                # mirror writes it BEFORE fan-out (SSE subscribers see
                # the link in real time).
                replies_to_v2_event_id=payload.replies_to_event_id,
                private_to_actors=payload.private_to_actors or [],
                expected_response=payload.expected_response,
            ),
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="operation conversation not found")
    except ChatActorIdentityError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Resolve the just-written v1 message to its mirrored v2 event id.
    repo = V2Repository()
    with session_scope() as db:
        from ..behaviors.chat.models import ChatMessageModel
        msg = db.get(ChatMessageModel, speech.id)
        v2_event_id = msg.v2_event_id if msg else None
        # Wire reply chain after the fact when the caller passed v2 ids:
        # no v1 column exists for the v2 reply id, so the mirror copy
        # gets it by post-update.
        if payload.replies_to_event_id and v2_event_id:
            ev = db.get(OperationEventV2Model, v2_event_id)
            if ev is not None and ev.replies_to_event_id is None:
                ev.replies_to_event_id = payload.replies_to_event_id
                db.flush()
        if not v2_event_id:
            raise HTTPException(
                status_code=500,
                detail="speech accepted but v2 mirror missing -- check dual-write",
            )
        # T1.2 — speech.evidence may carry a `payload.artifact` dict
        # describing a deliverable file (path/sha256/size/mime). The
        # bridge auto-creates an OperationArtifact row tied to this
        # event so collab-task ops have a formal audit trail of what
        # was produced. Other kinds ignore the field.
        #
        # P9.3 / D11 — also accept the *plural* ``payload.artifacts``
        # list form so a single evidence event can attach multiple
        # deliverables (exe + log + source code, for example). Both
        # forms may appear together; the bridge attaches every
        # normalized artifact in document order.
        if speech_kind == "evidence":
            from ..kernel.v2 import contract as _v2_contract
            from ..kernel.v2 import OperationMirror as _OperationMirror
            mirror = _OperationMirror()
            try:
                singular = _v2_contract.validate_artifact_payload(
                    payload.payload.get("artifact")
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid payload.artifact: {exc}",
                )
            try:
                plural = _v2_contract.validate_artifacts_list(
                    payload.payload.get("artifacts")
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid payload.artifacts: {exc}",
                )
            attach_list: list[dict] = []
            if singular is not None:
                attach_list.append(singular)
            if plural:
                attach_list.extend(plural)
            for art in attach_list:
                mirror.attach_artifact(
                    db,
                    v2_operation_id=operation_id,
                    v2_event_id=v2_event_id,
                    artifact=art,
                )
        # P9.1 / D9 — preserve `intent` on speech.ratify so the
        # quorum gate can distinguish close-intent from spec-ack
        # ratifies. The v1 mirror path strips arbitrary payload
        # keys (only `text` flows through), so we patch the v2
        # event's payload_json directly.
        if speech_kind == "ratify":
            intent = payload.payload.get("intent")
            if isinstance(intent, str) and intent.strip():
                import json as _json
                ev_row = db.get(OperationEventV2Model, v2_event_id)
                if ev_row is not None:
                    try:
                        existing = _json.loads(ev_row.payload_json or "{}")
                    except Exception:  # noqa: BLE001
                        existing = {}
                    if not isinstance(existing, dict):
                        existing = {}
                    existing["intent"] = intent.strip()
                    ev_row.payload_json = _json.dumps(existing, ensure_ascii=False)
                    db.flush()
        op = repo.get_operation(db, operation_id)
        ev = db.get(OperationEventV2Model, v2_event_id)
        # Discord visibility for v3 op events. The v1 chat path posts
        # to Discord via thread_manager; v3 callers (agents going
        # through /v2/operations) bypassed Discord entirely so the
        # parent thread stayed silent even while agents talked. Hook
        # here: look up the chat thread, format a human-readable
        # line, post via thread_manager. Best-effort — Discord errors
        # never break the event write. Skipped when discord is
        # disabled (the thread_manager itself short-circuits).
        from ..behaviors.chat.models import ChatConversationModel, ChatThreadModel
        v1_conv = db.get(ChatConversationModel, v1_id)
        discord_thread_id: str | None = None
        if v1_conv is not None:
            thread_row = db.get(ChatThreadModel, v1_conv.thread_id)
            if thread_row is not None:
                discord_thread_id = thread_row.discord_thread_id
        if discord_thread_id:
            handle = payload.actor_handle if payload.actor_handle.startswith("@") else f"@{payload.actor_handle}"
            # Strip the chat. prefix on the kind so display is concise:
            # ``chat.speech.propose`` → ``propose``.
            display_kind = payload.kind.split(".", 1)[1] if "." in payload.kind else payload.kind
            text = str(payload.payload.get("text", ""))
            artifact_meta = payload.payload.get("artifact")
            artifact_line = ""
            if isinstance(artifact_meta, dict) and artifact_meta.get("uri"):
                artifact_line = (
                    f"\n📎 artifact: `{artifact_meta.get('uri')}` "
                    f"({artifact_meta.get('size_bytes','?')} bytes, "
                    f"sha256={artifact_meta.get('sha256','?')[:12]}…)"
                )
            forwarded = f"**{handle}** _[{display_kind}]_\n{text}{artifact_line}"
            tm = services.thread_manager
            background_tasks.add_task(
                _post_to_discord_safely, tm, discord_thread_id, forwarded,
            )
        return _serialize_event(ev, repo) | {"operation_id": op.id}


async def _post_to_discord_safely(thread_manager, discord_thread_id: str, text: str) -> None:
    """Best-effort Discord forwarder. Logged + swallowed on any error so
    a flaky Discord client never blocks the bridge from accepting
    events. Discord-disabled mode short-circuits inside post_message
    itself."""
    try:
        await thread_manager.post_message(discord_thread_id, text)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "v3 → Discord forward failed for thread=%s: %r", discord_thread_id, exc,
        )


@router.post("/{operation_id}/close")
def close_operation(
    operation_id: str,
    payload: V2CloseRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
    x_actor_token: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_actor_handle_claim(
        request, claimed_handle=payload.actor_handle, x_actor_token=x_actor_token,
    )
    require_scope(request, x_actor_token=x_actor_token, needed=TOKEN_SCOPE_SPEAK)
    v1_id = _operation_to_v1_conversation_id(operation_id)
    services = request.app.state.services
    from ..behaviors.chat.conversation_service import (
        ChatConversationNotFoundError, ChatConversationStateError, ChatActorIdentityError,
    )
    try:
        services.chat_conversation_service.close_conversation(
            conversation_id=v1_id,
            closed_by=_strip_at(payload.actor_handle),
            resolution=payload.resolution,
            summary=payload.summary,
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="operation conversation not found")
    except ChatActorIdentityError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        # P9.5 — forward an op-closed marker so operators see the
        # lifecycle terminate in Discord. Best-effort.
        from ..behaviors.chat.models import ChatConversationModel, ChatThreadModel
        v1_conv = db.get(ChatConversationModel, v1_id)
        discord_thread_id = None
        if v1_conv is not None:
            thread_row = db.get(ChatThreadModel, v1_conv.thread_id)
            if thread_row is not None:
                discord_thread_id = thread_row.discord_thread_id
        if discord_thread_id:
            arts = repo.list_artifacts_for_operation(db, operation_id=op.id)
            n_events = len(repo.list_events(db, operation_id=op.id, limit=10000))
            forwarded = (
                f"✅ **op closed** _{op.kind}_ — `{op.title}`\n"
                f"resolution: **{op.resolution}** "
                f"by {payload.actor_handle}\n"
                f"events: {n_events} · artifacts: {len(arts)}"
            )
            if op.resolution_summary:
                forwarded += f"\n_summary:_ {op.resolution_summary[:300]}"
            background_tasks.add_task(
                _post_to_discord_safely,
                services.thread_manager, discord_thread_id, forwarded,
            )
        return _serialize_operation(op, repo)


# ---- H3: task lifecycle native endpoints ----
# All delegate to ChatTaskCoordinator. Same exception -> HTTP status
# mapping (404 not found / 403 actor identity / 400 state error).
def _coord_call(http_request: Request, fn_name: str, *args, **kwargs):
    services = http_request.app.state.services
    coord = getattr(services, "chat_task_coordinator", None)
    if coord is None:
        raise HTTPException(status_code=503, detail="task coordinator not available")
    from ..behaviors.chat.conversation_service import (
        ChatConversationNotFoundError, ChatConversationStateError, ChatActorIdentityError,
    )
    from ..behaviors.chat.task_coordinator import ChatTaskBindingError
    try:
        return getattr(coord, fn_name)(*args, **kwargs)
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="operation conversation not found")
    except ChatActorIdentityError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ChatTaskBindingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _coord_response_to_v2_dict(operation_id: str, coord_response) -> dict[str, Any]:
    """Map ChatTaskStateResponse -> v2 op dict + task fields.
    Caller already knows the op_id; we re-fetch for canonical state."""
    repo = V2Repository()
    with session_scope() as db:
        op = repo.get_operation(db, operation_id)
        body = _serialize_operation(op, repo)
    body["task"] = coord_response.task
    return body


@router.post("/{operation_id}/claim", status_code=201)
def claim_operation(
    operation_id: str,
    payload: V2ClaimRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    v1_id = _operation_to_v1_conversation_id(operation_id)
    from ..behaviors.chat.conversation_schemas import ChatTaskClaimRequest
    coord_response = _coord_call(
        request, "claim",
        conversation_id=v1_id,
        request=ChatTaskClaimRequest(
            actor_name=_strip_at(payload.actor_handle),
            lease_seconds=payload.lease_seconds,
        ),
    )
    return _coord_response_to_v2_dict(operation_id, coord_response)


@router.post("/{operation_id}/evidence", status_code=201)
def submit_evidence(
    operation_id: str,
    payload: V2EvidenceRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    v1_id = _operation_to_v1_conversation_id(operation_id)
    from ..behaviors.chat.conversation_schemas import ChatTaskEvidenceRequest
    coord_response = _coord_call(
        request, "add_evidence",
        conversation_id=v1_id,
        request=ChatTaskEvidenceRequest(
            actor_name=_strip_at(payload.actor_handle),
            lease_token=payload.lease_token,
            kind=payload.kind,
            summary=payload.summary,
            payload=payload.payload,
        ),
    )
    return _coord_response_to_v2_dict(operation_id, coord_response)


@router.post("/{operation_id}/approval/request", status_code=201)
def request_approval(
    operation_id: str,
    payload: V2ApprovalRequestRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    v1_id = _operation_to_v1_conversation_id(operation_id)
    from ..behaviors.chat.conversation_schemas import ChatTaskApprovalRequest
    coord_response = _coord_call(
        request, "request_approval",
        conversation_id=v1_id,
        request=ChatTaskApprovalRequest(
            actor_name=_strip_at(payload.actor_handle),
            lease_token=payload.lease_token,
            reason=payload.reason,
            note=payload.note,
        ),
    )
    return _coord_response_to_v2_dict(operation_id, coord_response)


@router.post("/{operation_id}/approval/resolve")
def resolve_approval(
    operation_id: str,
    payload: V2ApprovalResolveRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    v1_id = _operation_to_v1_conversation_id(operation_id)
    from ..behaviors.chat.conversation_schemas import ChatTaskApprovalResolveRequest
    coord_response = _coord_call(
        request, "resolve_approval",
        conversation_id=v1_id,
        request=ChatTaskApprovalResolveRequest(
            resolved_by=_strip_at(payload.actor_handle),
            resolution=payload.resolution,
            note=payload.note,
        ),
    )
    return _coord_response_to_v2_dict(operation_id, coord_response)


@router.post("/{operation_id}/complete")
def complete_operation(
    operation_id: str,
    payload: V2CompleteRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    v1_id = _operation_to_v1_conversation_id(operation_id)
    from ..behaviors.chat.conversation_schemas import ChatTaskCompleteRequest
    coord_response = _coord_call(
        request, "complete",
        conversation_id=v1_id,
        request=ChatTaskCompleteRequest(
            actor_name=_strip_at(payload.actor_handle),
            lease_token=payload.lease_token,
            summary=payload.summary,
        ),
    )
    return _coord_response_to_v2_dict(operation_id, coord_response)


@router.post("/{operation_id}/fail")
def fail_operation(
    operation_id: str,
    payload: V2FailRequest,
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    v1_id = _operation_to_v1_conversation_id(operation_id)
    from ..behaviors.chat.conversation_schemas import ChatTaskFailRequest
    coord_response = _coord_call(
        request, "fail",
        conversation_id=v1_id,
        request=ChatTaskFailRequest(
            actor_name=_strip_at(payload.actor_handle),
            lease_token=payload.lease_token,
            error_text=payload.error_text,
        ),
    )
    return _coord_response_to_v2_dict(operation_id, coord_response)
