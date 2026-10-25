"""Database engine and a small, dependency-free migration runner.

Migrations are plain SQL files in db/migrations, applied in filename order.
Each applied file is recorded in `schema_migrations` with a SHA-256 checksum,
so editing a migration after it has been applied is detected instead of
silently ignored. Schema changes always go in a new numbered file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

from ballast.config import MIGRATIONS_DIR, get_settings


class MigrationError(RuntimeError):
    pass


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    return create_engine(url, future=True)


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migrate(engine: Engine, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the filenames applied in this run."""
    files = sorted(migrations_dir.glob("*.sql"))
    applied_now: list[str] = []

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename   TEXT PRIMARY KEY,
                    checksum   TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        already = dict(conn.execute(text("SELECT filename, checksum FROM schema_migrations")).all())

    for path in files:
        sql = path.read_text(encoding="utf-8")
        checksum = _checksum(sql)
        if path.name in already:
            if already[path.name] != checksum:
                raise MigrationError(
                    f"{path.name} was changed after it was applied. "
                    "Put schema changes in a new migration file instead."
                )
            continue
        # One transaction per file: a failing migration leaves no partial schema behind.
        with engine.begin() as conn:
            # Run on the raw DBAPI cursor with no parameters, so SQL containing "%"
            # (e.g. LIKE 'model:%') is sent as-is instead of parsed for placeholders.
            cursor = conn.connection.cursor()
            try:
                cursor.execute(sql)
            finally:
                cursor.close()
            conn.execute(
                text("INSERT INTO schema_migrations (filename, checksum) VALUES (:f, :c)"),
                {"f": path.name, "c": checksum},
            )
        applied_now.append(path.name)

    return applied_now
