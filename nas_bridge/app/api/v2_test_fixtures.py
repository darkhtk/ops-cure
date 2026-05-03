"""v3 phase 5 — test-fixture endpoints.

Conformance suites need a way to provision the kinds of side state
(threads, in this chat-only era) that production deployments
ordinarily get from Discord. These endpoints are gated by
``BRIDGE_TEST_MODE=1`` and are NOT part of the v3 normative protocol
surface — they're labeled ``test-only`` in OpenAPI.

External implementers writing a conformance runner have two
options:

1. Run their bridge with ``BRIDGE_TEST_MODE=1`` so the conformance
   pack can call these endpoints.
2. Re-implement the same fixture surface (same paths, same shapes).
   The conformance pack is path-driven; it doesn't care about
   implementation specifics behind the path.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import BridgeCaller, require_bridge_caller
from ..db import session_scope

router = APIRouter(prefix="/v2/_test", tags=["test-only"])


def _gate() -> None:
    """Reject every test-fixture call when BRIDGE_TEST_MODE != 1."""
    if os.environ.get("BRIDGE_TEST_MODE", "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        raise HTTPException(
            status_code=404,
            detail="test fixture endpoints disabled (BRIDGE_TEST_MODE=1 required)",
        )


@router.post("/provision-thread", status_code=201)
def provision_thread(
    request: Request,
    caller: BridgeCaller = Depends(require_bridge_caller),  # noqa: ARG001
) -> dict[str, Any]:
    """Create a fresh chat thread row that conformance tests can scope
    operations into. Returns the canonical ``space_id`` (the
    ``discord_thread_id`` in the chat-only era) that subsequent
    POST /v2/operations calls expect.

    This is *not* part of the v3 protocol surface — it's a fixture
    used only by the conformance suite under BRIDGE_TEST_MODE=1.
    """
    _gate()
    from ..behaviors.chat.models import ChatThreadModel
    discord_id = "conformance-" + uuid.uuid4().hex[:12]
    with session_scope() as db:
        row = ChatThreadModel(
            id=str(uuid.uuid4()),
            guild_id="conformance",
            parent_channel_id="conformance",
            discord_thread_id=discord_id,
            title="conformance fixture",
            created_by="conformance",
        )
        db.add(row)
        db.flush()
        return {"space_id": discord_id, "thread_id": row.id}
