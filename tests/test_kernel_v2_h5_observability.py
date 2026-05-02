"""H5: agent metrics, digest scheduler, /v2/diagnostics."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from conftest import NAS_BRIDGE_ROOT


def _bootstrap(tmp_path, monkeypatch):
    if str(NAS_BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(NAS_BRIDGE_ROOT))
    monkeypatch.setenv("BRIDGE_SHARED_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRIDGE_DISABLE_DISCORD", "true")
    monkeypatch.setenv("BRIDGE_DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    import app.config as config
    config.get_settings.cache_clear()
    import app.db as db
    from app.behaviors.chat.conversation_service import ChatConversationService
    from app.behaviors.chat.conversation_schemas import (
        ConversationOpenRequest, SpeechActSubmitRequest,
    )
    from app.behaviors.chat.models import ChatThreadModel, ChatConversationModel
    from app.behaviors.agent import AgentRunner, EchoBrain
    from app.behaviors.digest import DigestService, DigestSchedulerLoop
    from app.kernel.subscriptions import InProcessSubscriptionBroker
    from app.kernel.v2 import V2Repository
    from app.main import app
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread, suffix="h5"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


_AUTH = {"Authorization": "Bearer t"}


# ---- agent metrics --------------------------------------------------------


def test_agent_runner_counters_increment_on_dispatch(tmp_path, monkeypatch):
    """envelopes_seen / brain_invocations / actions_delivered should
    bump as expected after a deterministic round."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"])
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="logs?",
            opener_actor="alice", addressed_to="claude-pca",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice",
            content="?", addressed_to="claude-pca",
        ),
    )
    runner = m["AgentRunner"](
        actor_handle="@claude-pca",
        brain=m["EchoBrain"](),
        broker=broker, chat_service=chat,
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        claude_id = repo.get_actor_by_handle(session, "@claude-pca").id
    backlog = list(broker._backlog.get(f"v2:inbox:{claude_id}", []))
    for env in backlog:
        runner.dispatch(env)

    metrics = runner.metrics
    assert metrics["envelopes_seen"] >= 2  # opened + question
    assert metrics["brain_invocations"] >= 1  # at least the question
    assert metrics["actions_delivered"] >= 1  # echo replied


def test_agent_runner_counts_skipped_self_envelopes(tmp_path, monkeypatch):
    """Self-authored envelope -> skipped_self bump, no brain invocation."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    broker = m["InProcessSubscriptionBroker"]()
    chat = m["ChatConversationService"](subscription_broker=broker)
    discord = _thread(db, m["ChatThreadModel"], suffix="self")
    summary = chat.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x",
            opener_actor="claude-pca", addressed_to="alice",
        ),
    )
    chat.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="claude-pca",
            content="hi alice", addressed_to="alice",
        ),
    )
    runner = m["AgentRunner"](
        actor_handle="@claude-pca",
        brain=m["EchoBrain"](),
        broker=broker, chat_service=chat,
    )
    repo = m["V2Repository"]()
    with db.session_scope() as session:
        claude_id = repo.get_actor_by_handle(session, "@claude-pca").id
    for env in list(broker._backlog.get(f"v2:inbox:{claude_id}", [])):
        runner.dispatch(env)
    # Both events were authored by claude-pca (themselves) -> all skipped.
    assert runner.metrics["skipped_self"] >= 2
    assert runner.metrics["brain_invocations"] == 0


# ---- digest scheduler ------------------------------------------------------


def test_digest_scheduler_fires_rollup_once(tmp_path, monkeypatch):
    """Direct call _fire_once on the loop; verify it posts a system
    speech to general for each space with closed ops in window."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    chat = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"], suffix="digest")
    chat.ensure_general(discord_thread_id=discord)
    # close 2 ops
    for title in ["op1", "op2"]:
        op = chat.open_conversation(
            discord_thread_id=discord,
            request=m["ConversationOpenRequest"](
                kind="inquiry", title=title, opener_actor="alice",
            ),
        )
        chat.close_conversation(
            conversation_id=op.id, closed_by="alice",
            resolution="answered",
        )

    loop = m["DigestSchedulerLoop"](
        chat_service=chat, interval_seconds=60,
        system_actor_handle="@digest-bot",
    )
    posted = loop._fire_once()
    assert posted == 1  # one space, both ops aggregate

    # Verify a system speech landed in general
    from sqlalchemy import select
    with db.session_scope() as s:
        from app.behaviors.chat.models import ChatMessageModel, ChatConversationModel
        general = s.scalar(
            select(ChatConversationModel)
            .where(ChatConversationModel.is_general.is_(True))
        )
        msgs = list(s.scalars(
            select(ChatMessageModel)
            .where(ChatMessageModel.conversation_id == general.id)
            .where(ChatMessageModel.actor_name == "digest-bot")
        ))
        assert len(msgs) >= 1
        assert "Daily digest" in msgs[0].content


# ---- /v2/diagnostics ------------------------------------------------------


def test_diagnostics_endpoint_returns_state_distribution(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    discord = _thread(m["db"], m["ChatThreadModel"], suffix="diag")
    with TestClient(m["app"]) as client:
        client.headers.update(_AUTH)
        # open 2 ops
        client.post(
            "/v2/operations",
            json={"space_id": discord, "kind": "inquiry",
                  "title": "q1", "opener_actor_handle": "@alice"},
        )
        client.post(
            "/v2/operations",
            json={"space_id": discord, "kind": "proposal",
                  "title": "p1", "opener_actor_handle": "@alice"},
        )
        # query diagnostics
        r = client.get("/v2/diagnostics")
        assert r.status_code == 200
        body = r.json()
        assert "operations" in body
        assert body["operations"]["total"] >= 3  # 2 + general
        # by_kind has at least our kinds
        kinds = body["operations"]["by_kind"]
        assert kinds.get("inquiry", 0) >= 1
        assert kinds.get("proposal", 0) >= 1
        # by_state should have 'open' for all so far
        assert body["operations"]["by_state"].get("open", 0) >= 3
        # broker section
        assert "broker" in body
        assert isinstance(body["broker"]["backlogs"], list)
        # agent_service is None unless BRIDGE_AGENT_ENABLED -> agents empty
        assert body["agents"] == []


def test_diagnostics_requires_auth(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    with TestClient(m["app"]) as client:
        r = client.get("/v2/diagnostics")
        assert r.status_code == 401
