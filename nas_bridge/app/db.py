from __future__ import annotations

from contextlib import contextmanager
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


def init_db() -> None:
    from . import models  # noqa: F401

    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _run_runtime_migrations()


def _run_runtime_migrations() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    migrations = {
        "sessions": [
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
        ],
        "agents": [
            ("desired_status", "TEXT NOT NULL DEFAULT 'ready'"),
            ("paused_reason", "TEXT"),
        ],
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table_name, columns in migrations.items():
            if table_name not in inspector.get_table_names():
                continue
            existing_columns = {
                column_info["name"]
                for column_info in inspector.get_columns(table_name)
            }
            for column_name, column_sql in columns:
                if column_name in existing_columns:
                    continue
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))


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

