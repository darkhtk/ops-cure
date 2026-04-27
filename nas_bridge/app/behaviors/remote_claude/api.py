"""Browser and agent-facing API surface for remote_claude.

Mirrors remote_codex's api.py — same auth deps, same SSE pattern, same
agent endpoints — but for the claude CLI's run/session model.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...auth import (
    BridgeCaller,
    build_bridge_audit_fields,
    require_bridge_permissions,
)

router = APIRouter(
    prefix="/api/remote-claude",
    tags=["remote_claude"],
)

ReadBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:read"))]
WriteBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:write"))]
StreamBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:stream"))]
ControlBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:control"))]


def _requested_by(caller: BridgeCaller) -> dict[str, Any]:
    return build_bridge_audit_fields(caller)


def _raise_for_error(error: Exception) -> None:
    detail = str(error)
    if detail in {"machine_not_found", "session_not_found", "command_not_found"}:
        raise HTTPException(status_code=404, detail=detail) from error
    if detail in {"machine_offline", "run_in_progress"}:
        raise HTTPException(status_code=409, detail=detail) from error
    raise HTTPException(status_code=400, detail=detail) from error


# ---- Browser-facing read endpoints --------------------------------------

@router.get("/machines")
async def list_machines(request: Request, _caller: ReadBridgeCaller) -> dict[str, Any]:
    return request.app.state.services.remote_claude_service.list_machines()


@router.get("/machines/{machine_id}/sessions")
async def list_sessions(
    machine_id: str,
    request: Request,
    _caller: ReadBridgeCaller,
    limit: int = 200,
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_claude_service.list_sessions(machine_id, limit=limit)
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.get("/machines/{machine_id}/sessions/{session_id}")
async def get_session(
    machine_id: str,
    session_id: str,
    request: Request,
    _caller: ReadBridgeCaller,
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_claude_service.get_session(machine_id, session_id)
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


# ---- Browser-driven commands --------------------------------------------

@router.post("/machines/{machine_id}/sessions")
async def start_run(
    machine_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    """Start a fresh claude run in the given cwd. Body: {cwd, prompt,
    attachments?, model?, permissionMode?}. Returns commandId; the agent
    fulfils the command and the resulting sessionId arrives via SSE +
    a follow-up sessions sync."""
    body = await request.json()
    cwd = str(body.get("cwd") or "").strip()
    prompt = str(body.get("prompt") or "")
    if not cwd:
        raise HTTPException(status_code=400, detail="missing_cwd")
    try:
        return request.app.state.services.remote_claude_service.enqueue_run_start(
            machine_id=machine_id,
            cwd=cwd,
            prompt=prompt,
            attachments=body.get("attachments") if isinstance(body.get("attachments"), list) else [],
            model=body.get("model"),
            permission_mode=body.get("permissionMode"),
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.post("/machines/{machine_id}/sessions/{session_id}/input")
async def append_input(
    machine_id: str,
    session_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    """Append a user message to a live (or resume-able) claude run."""
    body = await request.json()
    text = str(body.get("text") or "")
    if not text and not (isinstance(body.get("attachments"), list) and body.get("attachments")):
        raise HTTPException(status_code=400, detail="empty_message")
    try:
        return request.app.state.services.remote_claude_service.enqueue_run_input(
            machine_id=machine_id,
            session_id=session_id,
            run_id=body.get("runId"),
            text=text,
            attachments=body.get("attachments") if isinstance(body.get("attachments"), list) else [],
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.post("/machines/{machine_id}/sessions/{session_id}/interrupt")
async def interrupt_run(
    machine_id: str,
    session_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    try:
        return request.app.state.services.remote_claude_service.enqueue_run_interrupt(
            machine_id=machine_id,
            session_id=session_id,
            run_id=body.get("runId"),
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.delete("/machines/{machine_id}/sessions/{session_id}")
async def delete_session(
    machine_id: str,
    session_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_claude_service.enqueue_session_delete(
            machine_id=machine_id,
            session_id=session_id,
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.get("/machines/{machine_id}/fs/list")
async def fs_list(
    machine_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    path = request.query_params.get("path", "")
    try:
        return request.app.state.services.remote_claude_service.enqueue_fs_list(
            machine_id=machine_id, path=path, requested_by=_requested_by(caller)
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.post("/machines/{machine_id}/fs/mkdir")
async def fs_mkdir(
    machine_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    try:
        return request.app.state.services.remote_claude_service.enqueue_fs_mkdir(
            machine_id=machine_id,
            parent=str(body.get("parent") or ""),
            name=str(body.get("name") or ""),
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.post("/machines/{machine_id}/sessions/{session_id}/approval")
async def respond_approval(
    machine_id: str,
    session_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    """Browser-side decision for a claude PreToolUse approval prompt.
    Body: {approvalId, decision: "allow"|"deny"|"approved_for_session", reason?}.
    """
    body = await request.json()
    try:
        return request.app.state.services.remote_claude_service.enqueue_approval_respond(
            machine_id=machine_id,
            session_id=session_id,
            approval_id=str(body.get("approvalId") or ""),
            decision=str(body.get("decision") or "allow"),
            reason=body.get("reason"),
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.get("/commands/{command_id}")
async def get_command(command_id: str, request: Request, _caller: ReadBridgeCaller) -> dict[str, Any]:
    cmd = request.app.state.services.remote_claude_service.get_command(command_id)
    if cmd is None:
        raise HTTPException(status_code=404, detail="command_not_found")
    return {"command": cmd}


# ---- Live SSE -----------------------------------------------------------

@router.get("/machines/{machine_id}/sessions/{session_id}/live")
async def stream_session(
    machine_id: str,
    session_id: str,
    request: Request,
    _caller: StreamBridgeCaller,
) -> StreamingResponse:
    state_service = request.app.state.services.remote_claude_service.state_service

    async def event_stream():
        async with state_service.subscribe_session(machine_id, session_id) as queue:
            yield f"event: ready\ndata: {{}}\n\n"
            keepalive = 15.0
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except asyncio.TimeoutError:
                    yield ":keepalive\n\n"
                    continue
                event_kind = payload.get("kind") or "event"
                yield f"event: {event_kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/machines/{machine_id}/live")
async def stream_machine(
    machine_id: str,
    request: Request,
    _caller: StreamBridgeCaller,
) -> StreamingResponse:
    """Machine-scoped SSE feed: command lifecycle (fs.list / fs.mkdir /
    session.start completion), session.created / .updated, and machine
    status. Lets the browser drop the per-command polling loop on
    /commands/{id} and the per-session-list refresh polling that used to
    catch new sessions after a fresh chat.
    """
    state_service = request.app.state.services.remote_claude_service.state_service

    async def event_stream():
        async with state_service.subscribe_machine(machine_id) as queue:
            yield f"event: ready\ndata: {{}}\n\n"
            keepalive = 15.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except asyncio.TimeoutError:
                    yield ":keepalive\n\n"
                    continue
                event_kind = payload.get("kind") or "event"
                yield f"event: {event_kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---- Agent endpoints ----------------------------------------------------

@router.post("/agent/sync")
async def agent_sync(request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    try:
        return request.app.state.services.remote_claude_service.agent_sync(body)
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.post("/agent/commands/claim")
async def agent_claim(request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    machine_id = str(body.get("machineId") or "")
    worker_id = str(body.get("workerId") or "")
    if not machine_id or not worker_id:
        raise HTTPException(status_code=400, detail="missing_machine_or_worker")
    return request.app.state.services.remote_claude_service.agent_claim_command(
        machine_id, worker_id=worker_id
    )


@router.post("/agent/commands/{command_id}/result")
async def agent_report_result(
    command_id: str, request: Request, _caller: WriteBridgeCaller
) -> dict[str, Any]:
    body = await request.json()
    worker_id = str(body.get("workerId") or "")
    status = str(body.get("status") or "")
    try:
        return request.app.state.services.remote_claude_service.agent_report_command_result(
            command_id,
            worker_id=worker_id,
            status=status,
            result=body.get("result"),
            error=body.get("error"),
        )
    except (ValueError, RuntimeError) as e: _raise_for_error(e)


@router.post("/agent/events")
async def agent_publish_event(request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    """Agent forwards a stream-json event (or any session-scoped event) to
    the bridge. Bridge fans out to SSE subscribers.
    Body: {machineId, sessionId, event}.
    """
    body = await request.json()
    machine_id = str(body.get("machineId") or "")
    session_id = str(body.get("sessionId") or "")
    event = body.get("event") or {}
    if not machine_id or not session_id or not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="bad_event")
    request.app.state.services.remote_claude_service.agent_publish_event(
        machine_id=machine_id, session_id=session_id, event=event
    )
    return {"ok": True}
