"""Live behavior smoke -- spins up a real bridge in-process via
TestClient + lifespan, exercises every shipped behavior end-to-end,
prints what landed.

Designed for `python scripts/smoke_behaviors.py` (no pytest). Output
is the actual response bodies + diagnostics so a human can SEE what
each behavior does.

Covers:
  - v2 native open / event / close
  - inbox filter
  - digest auto-attach on close
  - agent (echo brain) auto-reply
  - diagnostics surface
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "nas_bridge"))


def main() -> int:
    fd, db_str = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_path = Path(db_str)
    db_path.unlink(missing_ok=True)
    os.environ["BRIDGE_SHARED_AUTH_TOKEN"] = "smoke-token"
    os.environ["BRIDGE_DISABLE_DISCORD"] = "true"
    os.environ["BRIDGE_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    # Enable the echo agent so we can see auto-reply happen.
    os.environ["BRIDGE_AGENT_ENABLED"] = "true"
    os.environ["BRIDGE_AGENT_HANDLE"] = "@bridge-agent"
    os.environ["BRIDGE_AGENT_BRAIN"] = "echo"

    import app.config as config
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app
    from app.behaviors.chat.models import ChatThreadModel
    from app.db import session_scope, init_db

    print("=" * 64)
    print(" LIVE BEHAVIOR SMOKE -- bridge in-process")
    print("=" * 64)

    init_db()
    # Seed a chat thread (Discord-side surface)
    with session_scope() as s:
        thread = ChatThreadModel(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d-smoke", title="smoke thread",
            created_by="alice",
        )
        s.add(thread); s.flush()
        discord_id = thread.discord_thread_id
    print(f"[seed] chat thread discord_id={discord_id}\n")

    headers = {"Authorization": "Bearer smoke-token"}
    with TestClient(app) as client:
        client.headers.update(headers)

        # 1. Diagnostics BEFORE any work
        diag_before = client.get("/v2/diagnostics").json()
        print("[1] /v2/diagnostics (initial)")
        print(f"    operations.total = {diag_before['operations']['total']}")
        print(f"    agents = {[a['actor_handle'] for a in diag_before['agents']]}")
        print()

        # 2. Open inquiry op addressed to @bridge-agent
        op_resp = client.post(
            "/v2/operations",
            json={
                "space_id": discord_id,
                "kind": "inquiry",
                "title": "smoke: where are last week's logs?",
                "addressed_to": "bridge-agent",
                "opener_actor_handle": "@alice",
            },
        )
        print("[2] POST /v2/operations (inquiry, addressed_to=bridge-agent)")
        print(f"    status={op_resp.status_code}, op_id={op_resp.json()['id']}")
        op_id = op_resp.json()["id"]
        print()

        # 3. alice asks the question (this triggers EchoBrain via broker)
        q_resp = client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@alice",
                "kind": "speech.question",
                "payload": {"text": "where are last week's logs?"},
                "addressed_to": "bridge-agent",
            },
        )
        print("[3] POST event (speech.question)")
        print(f"    seq={q_resp.json()['seq']}, kind={q_resp.json()['kind']}")
        print()

        # The runner is async, the dispatch happens on next broker tick.
        # In TestClient, lifespan is active -- runner is in run_forever loop
        # that we need to give a moment to process.
        # Wait synchronously for events to appear.
        import time
        deadline = time.time() + 5.0
        agent_replied = False
        while time.time() < deadline:
            events = client.get(
                f"/v2/operations/{op_id}/events",
                params={"actor_handle": "@alice"},
            ).json()
            kinds = [e["kind"] for e in events["events"]]
            if any(
                "echo:" in (e["payload"].get("text") or "")
                for e in events["events"]
                if e["kind"].startswith("chat.speech.")
            ):
                agent_replied = True
                break
            time.sleep(0.2)

        events = client.get(
            f"/v2/operations/{op_id}/events",
            params={"actor_handle": "@alice"},
        ).json()
        print("[4] GET /v2/operations/{id}/events (after agent had time to react)")
        for e in events["events"]:
            text = (e.get("payload") or {}).get("text", "")[:80]
            print(f"    seq={e['seq']:>2} kind={e['kind']:<28} actor={e['actor_id'][:8]:8}.. text={text!r}")
        print(f"    -> agent replied: {agent_replied}")
        print()

        # 5. Inbox check for bridge-agent
        inbox = client.get(
            "/v2/inbox", params={"actor_handle": "@bridge-agent"},
        ).json()
        print("[5] GET /v2/inbox?actor_handle=@bridge-agent")
        print(f"    items={len(inbox['items'])}")
        for it in inbox["items"]:
            print(f"      op {it['operation_id'][:8]}.. {it['kind']:8} state={it['state']:6} role={it['role']}")
        print()

        # 6. Close the op -- digest behavior should attach a summary artifact
        close_resp = client.post(
            f"/v2/operations/{op_id}/close",
            json={
                "actor_handle": "@alice",
                "resolution": "answered",
                "summary": "agent gave the answer",
            },
        ).json()
        print(f"[6] POST close -> state={close_resp['state']}, resolution={close_resp['resolution']}")

        # 7. Artifacts: digest's summary card should be there
        arts = client.get(f"/v2/operations/{op_id}/artifacts").json()
        print("[7] GET /v2/operations/{id}/artifacts")
        for a in arts["artifacts"]:
            print(f"    kind={a['kind']:8} mime={a['mime']:20} label={a.get('label', '')}")
        print()

        # 8. Final diagnostics
        diag_after = client.get("/v2/diagnostics").json()
        print("[8] /v2/diagnostics (final)")
        print(f"    operations.total = {diag_after['operations']['total']}")
        print(f"    by_state = {diag_after['operations']['by_state']}")
        print(f"    by_kind  = {diag_after['operations']['by_kind']}")
        for a in diag_after["agents"]:
            print(f"    agent {a['actor_handle']} metrics:")
            for k, v in sorted(a["metrics"].items()):
                print(f"      {k:30} = {v}")
        print()

    print("=" * 64)
    print(" SMOKE COMPLETE")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
