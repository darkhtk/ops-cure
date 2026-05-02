"""Digest behavior — summary artifact on close + space rollup compose."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

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
    from app.behaviors.digest import DigestService, ARTIFACT_KIND_SUMMARY
    from app.kernel.v2 import V2Repository
    db.init_db()
    return locals() | {"db": db}


def _thread(db, Thread, suffix="1"):
    with db.session_scope() as s:
        t = Thread(
            id=str(uuid.uuid4()), guild_id="g", parent_channel_id="p",
            discord_thread_id=f"d-{suffix}", title=f"t-{suffix}", created_by="alice",
        )
        s.add(t); s.flush()
        return t.discord_thread_id


# -------- close-time card -----------------------------------------------------


def test_close_attaches_summary_artifact(tmp_path, monkeypatch):
    """close 한 inquiry 가 즉시 v2 op 의 artifact 로 summary 카드를 갖는다."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"])
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="logs missing?", opener_actor="alice",
            addressed_to="claude-pca",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="question", actor_name="alice", content="where are last week's logs?",
            addressed_to="claude-pca",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="claude-pca", content="rotated. I'll fetch.",
        ),
    )
    svc.close_conversation(
        conversation_id=summary.id, closed_by="alice",
        resolution="answered", summary="rotated logs, fetched",
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], summary.id)
        artifacts = repo.list_artifacts_for_operation(
            session, operation_id=v1.v2_operation_id,
        )
        summaries = [a for a in artifacts if a.kind == m["ARTIFACT_KIND_SUMMARY"]]
        assert len(summaries) == 1
        s_art = summaries[0]
        assert s_art.mime == "application/json"
        assert s_art.uri.startswith("data:application/json;base64,")
        # metadata is the same dict that's encoded in the data URI
        import json as _json
        meta = _json.loads(s_art.metadata_json)
        assert meta["kind"] == "inquiry"
        assert meta["title"] == "logs missing?"
        assert meta["resolution"] == "answered"
        assert meta["resolution_summary"] == "rotated logs, fetched"
        assert meta["totals"]["speech"] == 2
        # opening_question captures the first speech.question
        assert "logs" in meta["opening_question"]


def test_summary_card_counts_whisper_without_exposing_text(tmp_path, monkeypatch):
    """whisper 는 totals.whispers 카운트에만 들어가고 본문은 안 나옴."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"], suffix="whisper")
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="proposal", title="something", opener_actor="alice",
        ),
    )
    svc.submit_speech(
        conversation_id=summary.id,
        request=m["SpeechActSubmitRequest"](
            kind="claim", actor_name="alice", content="psst secret",
            private_to_actors=["bob"],
        ),
    )
    svc.close_conversation(
        conversation_id=summary.id, closed_by="alice",
        resolution="withdrawn",
    )
    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], summary.id)
        artifacts = repo.list_artifacts_for_operation(
            session, operation_id=v1.v2_operation_id,
        )
        s_art = next(a for a in artifacts if a.kind == m["ARTIFACT_KIND_SUMMARY"])
        import json as _json
        meta = _json.loads(s_art.metadata_json)
        assert meta["totals"]["whispers"] == 1
        # Whisper text MUST NOT appear in the summary
        text_blob = _json.dumps(meta, ensure_ascii=False)
        assert "psst secret" not in text_blob


def test_summary_omitted_when_digest_disabled(tmp_path, monkeypatch):
    """digest_service=False (sentinel) 가 와이어를 끊는다 -- 다른 close
    경로 테스트가 의도치 않게 artifact 1개 끼는 걸 막는 escape hatch."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]

    # Use a no-op digest by passing None? Actually our default replaces
    # None with a real one. To disable, pass a no-op stub.
    class _NoOp:
        def record_close(self, *args, **kwargs):
            return None

    svc = m["ChatConversationService"](digest_service=_NoOp())
    discord = _thread(db, m["ChatThreadModel"], suffix="noop")
    summary = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="x", opener_actor="alice",
        ),
    )
    svc.close_conversation(
        conversation_id=summary.id, closed_by="alice", resolution="dropped",
    )
    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], summary.id)
        artifacts = repo.list_artifacts_for_operation(
            session, operation_id=v1.v2_operation_id,
        )
        assert not any(a.kind == m["ARTIFACT_KIND_SUMMARY"] for a in artifacts)


# -------- daily rollup --------------------------------------------------------


def test_compose_space_rollup_aggregates_yesterday(tmp_path, monkeypatch):
    """3 ops close, rollup 가 by_kind / by_resolution / items 정확하게 모음."""
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"], suffix="rollup")

    # Open + close 3 ops with different resolutions
    for title, kind, resolution in [
        ("inquiry-1", "inquiry", "answered"),
        ("inquiry-2", "inquiry", "dropped"),
        ("proposal-1", "proposal", "accepted"),
    ]:
        opened = svc.open_conversation(
            discord_thread_id=discord,
            request=m["ConversationOpenRequest"](
                kind=kind, title=title, opener_actor="alice",
            ),
        )
        svc.close_conversation(
            conversation_id=opened.id, closed_by="alice", resolution=resolution,
        )

    digest = m["DigestService"]()
    # Window: from 1h ago to 1h from now (everything we just did is in)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=1)
    until = now + timedelta(hours=1)

    # space_id of v2 ops is "chat:<thread_uuid>" (not discord_thread_id)
    with db.session_scope() as session:
        v1 = session.scalars(
            __import__(
                "sqlalchemy", fromlist=["select"]
            ).select(m["ChatConversationModel"]).where(
                m["ChatConversationModel"].title == "inquiry-1"
            )
        ).first()
        repo = m["V2Repository"]()
        op = repo.get_operation(session, v1.v2_operation_id)
        space_id = op.space_id

    with db.session_scope() as session:
        rollup = digest.compose_space_rollup(
            session, space_id=space_id, since=since, until=until,
        )
    assert rollup["total_closed"] == 3
    assert rollup["by_kind"] == {"inquiry": 2, "proposal": 1}
    assert rollup["by_resolution"] == {"answered": 1, "dropped": 1, "accepted": 1}
    titles = sorted(item["title"] for item in rollup["items"])
    assert titles == ["inquiry-1", "inquiry-2", "proposal-1"]


def test_render_rollup_markdown_is_human_readable(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    digest = m["DigestService"]()
    rollup = {
        "space_id": "chat:abc",
        "since": "2026-05-01T00:00:00+00:00",
        "until": "2026-05-02T00:00:00+00:00",
        "total_closed": 2,
        "by_kind": {"inquiry": 1, "task": 1},
        "by_resolution": {"answered": 1, "completed": 1},
        "items": [
            {"operation_id": "op-1", "kind": "inquiry", "title": "logs?",
             "resolution": "answered", "duration_seconds": 1234,
             "closed_at": "2026-05-01T03:30:00+00:00"},
            {"operation_id": "op-2", "kind": "task", "title": "patch",
             "resolution": "completed", "duration_seconds": 7200,
             "closed_at": "2026-05-01T15:00:00+00:00"},
        ],
    }
    md = digest.render_rollup_markdown(rollup)
    assert "Daily digest" in md
    assert "closed: **2**" in md
    assert "logs?" in md
    assert "patch" in md
    assert "answered" in md
    assert "1234s" in md


def test_compose_rollup_excludes_ops_outside_window(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    db = m["db"]
    svc = m["ChatConversationService"]()
    discord = _thread(db, m["ChatThreadModel"], suffix="window")

    opened = svc.open_conversation(
        discord_thread_id=discord,
        request=m["ConversationOpenRequest"](
            kind="inquiry", title="recent", opener_actor="alice",
        ),
    )
    svc.close_conversation(
        conversation_id=opened.id, closed_by="alice", resolution="answered",
    )

    repo = m["V2Repository"]()
    with db.session_scope() as session:
        v1 = session.get(m["ChatConversationModel"], opened.id)
        op = repo.get_operation(session, v1.v2_operation_id)
        space_id = op.space_id

    digest = m["DigestService"]()
    # Window in the future -- should miss the recent close
    now = datetime.now(timezone.utc)
    far_future_since = now + timedelta(days=10)
    far_future_until = now + timedelta(days=11)

    with db.session_scope() as session:
        rollup = digest.compose_space_rollup(
            session, space_id=space_id,
            since=far_future_since, until=far_future_until,
        )
    assert rollup["total_closed"] == 0
    assert rollup["items"] == []
