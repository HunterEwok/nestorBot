"""Minimal forward-only migration runner: applies migrations/NNNN_*.sql in order.

Usage: python -m nestor.migrate   (reads DATABASE_URL, must connect as the owner role)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_LOCK_KEY = 0x6E6573746F72  # arbitrary, "nestor"

log = logging.getLogger(__name__)


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """The migrations in the order they are applied; the version is the file name without .sql."""
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))


async def migrate(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; returns the versions applied."""
    await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                 version    TEXT PRIMARY KEY,
                 applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        applied = []
        for path in migration_files(directory):
            version = path.stem
            if version in done:
                continue
            try:
                async with conn.transaction():
                    # A table held by someone else (a long dump, a stuck backend): give up after 30 s and
                    # let systemd start again later, instead of waiting with every writer queued behind.
                    await conn.execute("SET LOCAL lock_timeout = '30s'")
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
            except asyncpg.LockNotAvailableError:
                log.warning("migration waiting for lock", extra={"version": version})
                raise
            log.info("migration applied", extra={"version": version})
            applied.append(version)
        return applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)


async def _main() -> None:
    import os

    from dotenv import load_dotenv

    from .logs import setup_logging

    load_dotenv()
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        dsn = os.environ["DATABASE_URL"]  # only the database: no bot token needed to migrate
    except KeyError:
        raise SystemExit("missing required environment variable DATABASE_URL") from None
    conn = await asyncpg.connect(dsn)
    try:
        applied = await migrate(conn)
        log.info("migrations done", extra={"applied": applied})
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
