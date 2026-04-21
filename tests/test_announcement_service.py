from __future__ import annotations

from sqlalchemy import select

from test_orchestration_workflows import _start_session


def test_start_creates_status_card(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    from app.models import SessionModel

    with app_env.db.session_scope() as db:
        session_row = db.scalar(select(SessionModel).where(SessionModel.id == summary.id))
        assert session_row is not None
        assert session_row.status_message_id is not None
        message = app_env.thread_manager.message_store[session_row.status_message_id]

    assert message[0] == summary.discord_thread_id
    assert "**Opscure Status**" in message[1]
    assert "Target: `UlalaCheese`" in message[1]
    assert "Profile: `UlalaCheese`" in message[1]
    assert "State: `waiting_for_workers`" in message[1]


def test_register_worker_updates_status_card(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="planner",
            worker_id="worker-planner",
            pid_hint=1001,
        ),
    )
    __import__("asyncio").run(
        app_env.session_service.register_worker(
            session_id=summary.id,
            agent_name="coder",
            worker_id="worker-coder",
            pid_hint=1002,
        ),
    )

    from app.models import SessionModel

    with app_env.db.session_scope() as db:
        session_row = db.scalar(select(SessionModel).where(SessionModel.id == summary.id))
        assert session_row is not None
        assert session_row.status_message_id is not None
        message = app_env.thread_manager.message_store[session_row.status_message_id]

    assert app_env.thread_manager.edited_messages
    assert "State: `ready`" in message[1]
    assert "Workers: 2/2 attached" in message[1]
    assert "Next: waiting for your next instruction" in message[1]


def test_render_session_status_text_uses_status_card_format(app_env):
    summary = __import__("asyncio").run(_start_session(app_env))

    text = __import__("asyncio").run(
        app_env.session_service.render_session_status_text(summary.id),
    )

    assert "**Opscure Status**" in text
    assert "Queue: pending=0, active=0" in text
    assert "Policy: parallel=1, auto_retry=True" in text
