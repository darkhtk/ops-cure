"""Browser and agent-facing API surface for remote_codex."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ...auth import (
    BridgeCaller,
    build_bridge_audit_fields,
    require_bridge_permissions,
)

router = APIRouter(
    prefix="/api/remote-codex",
    tags=["remote_codex"],
)

ReadBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:read"))]
WriteBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:write"))]
StreamBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:stream"))]
ControlBridgeCaller = Annotated[BridgeCaller, Depends(require_bridge_permissions("bridge:control"))]


def _requested_by(caller: BridgeCaller) -> dict[str, Any]:
    return build_bridge_audit_fields(caller)


def _raise_for_error(error: Exception) -> None:
    detail = str(error)
    if detail == "machine_not_found":
        raise HTTPException(status_code=404, detail=detail) from error
    if detail == "thread_not_found":
        raise HTTPException(status_code=404, detail=detail) from error
    if detail in {
        "machine_offline",
        "machine_live_control_unavailable",
        "turn_command_in_progress",
        "turn_in_progress",
        "interrupt_command_in_progress",
    }:
        raise HTTPException(status_code=409, detail=detail) from error
    if detail in {"missing_turn_id", "task_not_claimed"}:
        raise HTTPException(status_code=400, detail=detail) from error
    raise HTTPException(status_code=400, detail=detail) from error


@router.get("/health")
async def health(request: Request, _caller: ReadBridgeCaller) -> dict[str, Any]:
    return request.app.state.services.remote_codex_service.get_health()


@router.get("/control-status")
async def control_status(request: Request, _caller: ReadBridgeCaller) -> dict[str, Any]:
    return request.app.state.services.remote_codex_service.get_control_status()


@router.get("/machines")
async def list_machines(request: Request, _caller: ReadBridgeCaller) -> dict[str, Any]:
    return request.app.state.services.remote_codex_service.list_machines()


@router.get("/machines/{machine_id}/threads")
async def list_machine_threads(
    machine_id: str,
    request: Request,
    _caller: ReadBridgeCaller,
    q: str | None = Query(default=""),
    limit: int = Query(default=60, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_codex_service.list_machine_threads(
            machine_id,
            query=q or "",
            limit=limit,
        )
    except ValueError as error:
        _raise_for_error(error)


@router.get("/machines/{machine_id}/threads/{thread_id}")
async def get_thread(
    machine_id: str,
    thread_id: str,
    request: Request,
    _caller: ReadBridgeCaller,
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_codex_service.get_thread(machine_id, thread_id)
    except ValueError as error:
        _raise_for_error(error)


@router.get("/machines/{machine_id}/threads/{thread_id}/messages")
async def get_thread_messages(
    machine_id: str,
    thread_id: str,
    request: Request,
    _caller: ReadBridgeCaller,
    limit: int = Query(default=250, ge=0, le=1000),
    afterLineNumber: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_codex_service.get_thread_messages(
            machine_id,
            thread_id,
            limit=limit,
            after_line_number=afterLineNumber,
        )
    except ValueError as error:
        _raise_for_error(error)


@router.get("/machines/{machine_id}/threads/{thread_id}/commands")
async def get_thread_commands(
    machine_id: str,
    thread_id: str,
    request: Request,
    _caller: ReadBridgeCaller,
    limit: int = Query(default=8, ge=1, le=30),
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_codex_service.get_thread_commands(
            machine_id,
            thread_id,
            limit=limit,
        )
    except ValueError as error:
        _raise_for_error(error)


@router.get("/machines/{machine_id}/threads/{thread_id}/tasks")
async def get_thread_tasks(
    machine_id: str,
    thread_id: str,
    request: Request,
    _caller: ReadBridgeCaller,
    status: list[str] | None = Query(default=None),
    limit: int = Query(default=8, ge=1, le=30),
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_codex_service.list_thread_tasks(
            machine_id,
            thread_id,
            statuses=status,
            limit=limit,
        )
    except ValueError as error:
        _raise_for_error(error)


@router.post("/machines/{machine_id}/threads/{thread_id}/turns")
async def start_turn(
    machine_id: str,
    thread_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="missing_prompt")
    try:
        return request.app.state.services.remote_codex_service.enqueue_turn(
            machine_id=machine_id,
            thread_id=thread_id,
            prompt=prompt,
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as error:
        _raise_for_error(error)


@router.post("/machines/{machine_id}/threads/{thread_id}/interrupt")
async def interrupt_turn(
    machine_id: str,
    thread_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    try:
        return request.app.state.services.remote_codex_service.enqueue_interrupt(
            machine_id=machine_id,
            thread_id=thread_id,
            turn_id=body.get("turnId"),
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as error:
        _raise_for_error(error)


@router.delete("/machines/{machine_id}/threads/{thread_id}")
async def delete_thread(
    machine_id: str,
    thread_id: str,
    request: Request,
    caller: ControlBridgeCaller,
) -> dict[str, Any]:
    try:
        return request.app.state.services.remote_codex_service.enqueue_thread_delete(
            machine_id=machine_id,
            thread_id=thread_id,
            requested_by=_requested_by(caller),
        )
    except (ValueError, RuntimeError) as error:
        _raise_for_error(error)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request, _caller: ReadBridgeCaller) -> dict[str, Any]:
    return request.app.state.services.remote_codex_service.get_task(task_id)


@router.post("/tasks")
async def create_task(request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskCreateRequest

    payload = RemoteTaskCreateRequest(**body)
    return request.app.state.services.remote_codex_service.create_task(payload)


@router.post("/tasks/{task_id}/claim")
async def claim_task(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskClaimRequest

    payload = RemoteTaskClaimRequest(**body)
    return request.app.state.services.remote_codex_service.claim_task(task_id, payload)


@router.post("/machines/{machine_id}/tasks/claim-next")
async def claim_next_machine_task(
    machine_id: str,
    request: Request,
    _caller: ControlBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskClaimNextRequest

    payload = RemoteTaskClaimNextRequest(**body)
    claimed = request.app.state.services.remote_codex_service.claim_next_machine_task(
        machine_id=machine_id,
        payload=payload,
    )
    return claimed or {"task": None}


@router.post("/tasks/{task_id}/heartbeat")
async def heartbeat_task(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskHeartbeatRequest

    payload = RemoteTaskHeartbeatRequest(**body)
    return request.app.state.services.remote_codex_service.heartbeat_task(task_id, payload)


@router.post("/tasks/{task_id}/evidence")
async def add_evidence(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskEvidenceRequest

    payload = RemoteTaskEvidenceRequest(**body)
    return request.app.state.services.remote_codex_service.add_evidence(task_id, payload)


@router.post("/tasks/{task_id}/approval")
async def request_approval(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskApprovalRequest

    payload = RemoteTaskApprovalRequest(**body)
    return request.app.state.services.remote_codex_service.request_approval(task_id, payload)


@router.post("/tasks/{task_id}/approval/resolve")
async def resolve_approval(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    resolution = str(body.get("status") or body.get("resolution") or "").strip().lower()
    if resolution == "rejected":
        resolution = "denied"
    from ...schemas import RemoteTaskApprovalResolveRequest

    payload = RemoteTaskApprovalResolveRequest(
        resolved_by=str(body.get("resolvedBy") or body.get("resolved_by") or "browser"),
        resolution=resolution,
        note=body.get("note"),
    )
    return request.app.state.services.remote_codex_service.resolve_approval(task_id, payload)


@router.post("/tasks/{task_id}/notes")
async def add_note(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskNoteRequest

    payload = RemoteTaskNoteRequest(**body)
    return request.app.state.services.remote_codex_service.add_note(task_id, payload)


@router.get("/tasks/{task_id}/notes")
async def list_notes(task_id: str, request: Request, _caller: ReadBridgeCaller) -> dict[str, Any]:
    return request.app.state.services.remote_codex_service.list_notes(task_id)


@router.post("/tasks/{task_id}/interrupt")
async def interrupt_task(task_id: str, request: Request, _caller: ControlBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskInterruptRequest

    payload = RemoteTaskInterruptRequest(**body)
    return request.app.state.services.remote_codex_service.interrupt_task(task_id, payload)


@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskCompleteRequest

    payload = RemoteTaskCompleteRequest(**body)
    return request.app.state.services.remote_codex_service.complete_task(task_id, payload)


@router.post("/tasks/{task_id}/fail")
async def fail_task(task_id: str, request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    from ...schemas import RemoteTaskFailRequest

    payload = RemoteTaskFailRequest(**body)
    return request.app.state.services.remote_codex_service.fail_task(task_id, payload)


@router.post("/agent/sync")
async def agent_sync(request: Request, _caller: WriteBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    return request.app.state.services.remote_codex_service.apply_agent_sync(
        machine=body.get("machine") or {},
        threads=list(body.get("threads") or []),
        snapshots=list(body.get("snapshots") or []),
    )


@router.post("/agent/commands/claim")
async def agent_claim_next_command(request: Request, _caller: ControlBridgeCaller) -> dict[str, Any]:
    body = await request.json()
    return request.app.state.services.remote_codex_service.claim_next_command(
        machine_id=str(body.get("machineId") or body.get("machine_id") or ""),
        worker_id=str(body.get("workerId") or body.get("worker_id") or "unknown-worker"),
    )


@router.post("/agent/commands/{command_id}/result")
async def agent_command_result(
    command_id: str,
    request: Request,
    _caller: WriteBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    return request.app.state.services.remote_codex_service.record_command_result(
        command_id,
        worker_id=str(body.get("workerId") or body.get("worker_id") or "unknown-worker"),
        status=str(body.get("status") or ""),
        result=body.get("result"),
        error=body.get("error"),
    )


@router.post("/agent/tasks/{task_id}/heartbeat")
async def agent_task_heartbeat(
    task_id: str,
    request: Request,
    _caller: WriteBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    try:
        return request.app.state.services.remote_codex_service.agent_heartbeat_task(
            task_id,
            actor_id=str(body.get("actorId") or body.get("actor_id") or ""),
            phase=str(body.get("phase") or "claimed"),
            summary=body.get("summary"),
            commands_run_count=int(body.get("commandsRunCount") or 0),
            files_read_count=int(body.get("filesReadCount") or 0),
            files_modified_count=int(body.get("filesModifiedCount") or 0),
            tests_run_count=int(body.get("testsRunCount") or 0),
        )
    except ValueError as error:
        _raise_for_error(error)


@router.post("/agent/tasks/{task_id}/evidence")
async def agent_task_evidence(
    task_id: str,
    request: Request,
    _caller: WriteBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    return request.app.state.services.remote_codex_service.agent_add_evidence(
        task_id,
        actor_id=str(body.get("actorId") or body.get("actor_id") or ""),
        kind=str(body.get("kind") or "note"),
        summary=str(body.get("summary") or ""),
        payload=body.get("payload") or {},
    )


@router.post("/agent/tasks/{task_id}/complete")
async def agent_task_complete(
    task_id: str,
    request: Request,
    _caller: WriteBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    try:
        return request.app.state.services.remote_codex_service.agent_complete_task(
            task_id,
            actor_id=str(body.get("actorId") or body.get("actor_id") or ""),
            summary=body.get("summary"),
        )
    except ValueError as error:
        _raise_for_error(error)


@router.post("/agent/tasks/{task_id}/fail")
async def agent_task_fail(
    task_id: str,
    request: Request,
    _caller: WriteBridgeCaller,
) -> dict[str, Any]:
    body = await request.json()
    try:
        return request.app.state.services.remote_codex_service.agent_fail_task(
            task_id,
            actor_id=str(body.get("actorId") or body.get("actor_id") or ""),
            error=body.get("error") or {},
        )
    except ValueError as error:
        _raise_for_error(error)


@router.get("/machines/{machine_id}/threads/{thread_id}/live")
async def stream_thread(
    machine_id: str,
    thread_id: str,
    request: Request,
    _caller: StreamBridgeCaller,
    afterLineNumber: int = Query(default=0, ge=0),
):
    service = request.app.state.services.remote_codex_service
    handle = await service.subscribe_thread(machine_id, thread_id)

    async def event_stream():
        last_line_number = max(0, int(afterLineNumber))
        try:
            initial_snapshot = service.get_thread_messages(machine_id, thread_id, limit=0, after_line_number=last_line_number)
            initial_tasks = service.list_thread_tasks(machine_id, thread_id, limit=8)["tasks"]
        except ValueError as error:
            yield f"event: error\ndata: {json.dumps({'message': str(error)})}\n\n"
            handle.unsubscribe()
            return

        yield f"event: ready\ndata: {json.dumps({'machineId': machine_id, 'threadId': thread_id, 'totalMessages': initial_snapshot['totalMessages'], 'afterLineNumber': last_line_number, 'tasks': initial_tasks})}\n\n"
        yield f"event: state\ndata: {json.dumps({'machine': initial_snapshot['machine'], 'thread': initial_snapshot['thread'], 'totalMessages': initial_snapshot['totalMessages'], 'lineCount': initial_snapshot['lineCount'], 'fileSize': initial_snapshot['fileSize'], 'syncedAt': initial_snapshot['syncedAt']})}\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                payload = await asyncio.wait_for(handle.queue.get(), timeout=15)
            except TimeoutError:
                yield f"event: ping\ndata: {json.dumps({'at': int(asyncio.get_running_loop().time() * 1000)})}\n\n"
                continue
            kind = payload.get("kind")
            if kind in {"snapshot", "thread"}:
                snapshot = service.get_thread_messages(machine_id, thread_id, limit=0, after_line_number=last_line_number)
                if snapshot is None:
                    yield f"event: error\ndata: {json.dumps({'message': 'thread_not_found'})}\n\n"
                    continue
                fresh_messages = snapshot["messages"]
                if fresh_messages:
                    last_line_number = max(last_line_number, max(int(item.get("lineNumber") or 0) for item in fresh_messages))
                    yield f"event: messages\ndata: {json.dumps(fresh_messages)}\n\n"
                yield f"event: state\ndata: {json.dumps({'machine': snapshot['machine'], 'thread': snapshot['thread'], 'totalMessages': snapshot['totalMessages'], 'lineCount': snapshot['lineCount'], 'fileSize': snapshot['fileSize'], 'syncedAt': snapshot['syncedAt']})}\n\n"
            elif kind == "machine":
                yield f"event: machine\ndata: {json.dumps(payload['machine'])}\n\n"
            elif kind == "command":
                command = payload["command"]
                yield f"event: command\ndata: {json.dumps(command)}\n\n"
                if command.get("type") == "turn.start":
                    snapshot = service.get_thread_messages(machine_id, thread_id, limit=0, after_line_number=last_line_number)
                    fresh_messages = snapshot["messages"]
                    if fresh_messages:
                        last_line_number = max(
                            last_line_number,
                            max(int(item.get("lineNumber") or 0) for item in fresh_messages),
                        )
                        yield f"event: messages\ndata: {json.dumps(fresh_messages)}\n\n"
            elif kind == "task":
                yield f"event: task\ndata: {json.dumps(payload['task'])}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")
