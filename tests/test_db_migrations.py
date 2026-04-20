from __future__ import annotations

from sqlalchemy import select


def test_init_db_records_named_migrations(app_env):
    from app.models import SchemaMigrationModel

    with app_env.db.session_scope() as db:
        applied = list(db.scalars(select(SchemaMigrationModel.name).order_by(SchemaMigrationModel.name.asc())))

    assert "20260420_agent_pause_fields" in applied
    assert "20260420_session_orchestration_fields" in applied
