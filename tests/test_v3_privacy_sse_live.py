"""v3 phase 4 — multi-actor SSE privacy verification.

Whisper redaction is wired in two places:
  - ``_publish_v2_inbox_fanout`` (skips fan-out to non-recipients)
  - ``/v2/operations/{id}/events`` GET (filters in serialization)

The persona live exercise covered the GET path. This file pins down
the **fan-out** path: it inspects the in-process subscription broker
directly (the same call ``/v2/inbox/stream`` SSE wraps) so the same
queues that feed real SSE consumers are the ones we assert against.
We avoid live SSE+threading because TestClient's sync streaming
deadlocks under the same-thread broker push.
"""
from __future__ import annotations

import json
import sys
import uuid

import pytest

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setenv("BRIDGE_POLICY_SWEEPER_SECONDS", "0")
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    db.init_db()
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
    )
    from app.behaviors.chat.models import (
        ChatThreadModel, ChatConversationModel, ChatMessageModel,
    )
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.kernel.v2 import V2Repository
    from app.kernel.v2.actor_service import ActorService
    return locals()


def _thread(db, Thread):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id="d-privacy-sse", title="t", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


def test_carol_inbox_subscription_does_not_receive_whisper_to_bob(tmp_path, monkeypatch):
    """alice whispers to bob → ``v2:inbox:<carol_id>`` queue gets the
    public followup but NOT the whisper. Same broker code path as
    ``GET /v2/inbox/stream``."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])

    # Open op + add bob + carol as participants by addressing them
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="t", opener_actor="alice",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            actor_name="alice", kind="question",
            content="hi all", addressed_to_many=["bob", "carol"],
        ),
    )

    # Resolve bob + carol actor ids so we know which space to subscribe.
    repo = m["V2Repository"]()
    with db.session_scope() as s:
        bob = m["ActorService"](repo).ensure_actor_by_handle(s, handle="@bob")
        carol = m["ActorService"](repo).ensure_actor_by_handle(s, handle="@carol")
        bob_id, carol_id = bob.id, carol.id

    bob_sub = broker.subscribe(
        space_id=f"v2:inbox:{bob_id}", subscriber_id="probe-bob",
    )
    carol_sub = broker.subscribe(
        space_id=f"v2:inbox:{carol_id}", subscriber_id="probe-carol",
    )
    try:
        # alice whispers to bob (private)
        chat.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                actor_name="alice", kind="claim",
                content="WHISPER for bob only",
                private_to_actors=["bob"],
            ),
        )
        # public followup so we can confirm carol's subscription is alive
        chat.submit_speech(
            conversation_id=summary.id,
            request=m["SpeechActSubmitRequest"](
                actor_name="alice", kind="claim",
                content="public followup",
            ),
        )

        bob_envelopes = list(_drain(bob_sub))
        carol_envelopes = list(_drain(carol_sub))
    finally:
        bob_sub.close()
        carol_sub.close()

    bob_texts = [_text_of(e) for e in bob_envelopes]
    carol_texts = [_text_of(e) for e in carol_envelopes]

    # Sanity: bob got both events
    assert any("WHISPER" in t for t in bob_texts), (
        f"bob's broker queue missing whisper: {bob_texts}"
    )
    assert any("public followup" in t for t in bob_texts)
    # Sanity: carol's queue is alive
    assert any("public followup" in t for t in carol_texts), (
        f"carol's broker queue didn't receive any public event: {carol_texts}"
    )
    # Privacy: carol does NOT see the whisper
    assert not any("WHISPER" in t for t in carol_texts), (
        f"FAN-OUT PRIVACY LEAK: carol's queue received whisper: {carol_texts}"
    )


def _drain(subscription):
    """Pull every immediately-available envelope from the broker
    subscription. ``BrokerSubscription.queue`` is an asyncio.Queue
    whose ``get_nowait`` works synchronously; the broker's
    ``put_nowait`` filled it from inside ``submit_speech`` so by the
    time we get here every envelope is already present."""
    out = []
    q = subscription.queue
    while True:
        try:
            out.append(q.get_nowait())
        except Exception:  # noqa: BLE001 - asyncio.QueueEmpty
            break
    return out


def _text_of(envelope) -> str:
    """Pull the speech text out of a wrapped envelope. The wrapper
    JSON has shape ``{operation_id, payload: {text, ...}, ...}``."""
    try:
        wrapped = json.loads(envelope.event.content)
    except (ValueError, TypeError):
        return ""
    payload = wrapped.get("payload") or {}
    if isinstance(payload, dict):
        return payload.get("text") or ""
    return str(payload)
