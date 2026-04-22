from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@dataclass(frozen=True, slots=True)
class NamedMigration:
    name: str
    statements: tuple[str, ...]


def init_db() -> None:
    from . import models  # noqa: F401
    from .behaviors.chat import models as _chat_models  # noqa: F401
    from .behaviors.ops import models as _ops_models  # noqa: F401

    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _run_named_migrations()


def _run_named_migrations() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as connection:
        _ensure_schema_migrations_table(connection)
        applied = {
            row[0]
            for row in connection.execute(text("SELECT name FROM schema_migrations"))
        }
        for migration in _named_migrations(connection):
            if migration.name in applied:
                continue
            for statement in migration.statements:
                connection.execute(text(statement))
            connection.execute(
                text(
                    "INSERT INTO schema_migrations (name, applied_at) "
                    "VALUES (:name, CURRENT_TIMESTAMP)",
                ),
                {"name": migration.name},
            )


def _ensure_schema_migrations_table(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
            """,
        ),
    )


def _named_migrations(connection) -> tuple[NamedMigration, ...]:
    inspector = inspect(connection)

    def missing_columns(table_name: str, column_specs: list[tuple[str, str]]) -> tuple[str, ...]:
        if table_name not in inspector.get_table_names():
            return ()
        existing_columns = {
            column_info["name"]
            for column_info in inspector.get_columns(table_name)
        }
        return tuple(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
            for column_name, column_sql in column_specs
            if column_name not in existing_columns
        )

    return (
        NamedMigration(
            name="20260420_session_orchestration_fields",
            statements=missing_columns(
                "sessions",
                [
                    ("target_project_name", "TEXT"),
                    ("power_target_name", "TEXT"),
                    ("execution_target_name", "TEXT"),
                    ("desired_status", "TEXT NOT NULL DEFAULT 'ready'"),
                    ("power_state", "TEXT NOT NULL DEFAULT 'unknown'"),
                    ("execution_state", "TEXT NOT NULL DEFAULT 'unknown'"),
                    ("pause_reason", "TEXT"),
                    ("last_recovery_at", "DATETIME"),
                    ("last_recovery_reason", "TEXT"),
                    ("policy_version", "INTEGER NOT NULL DEFAULT 1"),
                    ("status_message_id", "TEXT"),
                    ("last_announced_state_hash", "TEXT"),
                    ("last_announced_at", "DATETIME"),
                ],
            ),
        ),
        NamedMigration(
            name="20260420_agent_pause_fields",
            statements=missing_columns(
                "agents",
                [
                    ("desired_status", "TEXT NOT NULL DEFAULT 'ready'"),
                    ("paused_reason", "TEXT"),
                ],
            ),
        ),
        NamedMigration(
            name="20260420_session_announcement_fields",
            statements=missing_columns(
                "sessions",
                [
                    ("status_message_id", "TEXT"),
                    ("last_announced_state_hash", "TEXT"),
                    ("last_announced_at", "DATETIME"),
                ],
            ),
        ),
        NamedMigration(
            name="20260421_state_kernel_columns",
            statements=
            missing_columns(
                "sessions",
                [
                    ("session_epoch", "INTEGER NOT NULL DEFAULT 1"),
                ],
            )
            + missing_columns(
                "jobs",
                [
                    ("task_id", "TEXT"),
                    ("handoff_id", "TEXT"),
                    ("session_epoch", "INTEGER NOT NULL DEFAULT 1"),
                    ("task_revision", "INTEGER NOT NULL DEFAULT 0"),
                    ("lease_token", "TEXT"),
                    ("idempotency_key", "TEXT"),
                ],
            ),
        ),
        NamedMigration(
            name="20260421_agent_activity_fields",
            statements=missing_columns(
                "agents",
                [
                    ("current_activity_line", "TEXT"),
                    ("current_activity_updated_at", "DATETIME"),
                ],
            ),
        ),
    )


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

