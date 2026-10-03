"""One-time database setup, run with superuser rights: roles and the database.

Tables come from migrations (python -m nestor.migrate). Safe to re-run: existing
roles get their passwords updated, an existing database is kept.

Usage: python -m nestor.bootstrap   (reads PG_ADMIN_URL, COLLECTOR_PASSWORD, READER_PASSWORD
                                     and optionally NESTOR_DB from the environment or .env)
"""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlsplit, urlunsplit

import asyncpg

log = logging.getLogger(__name__)

COLLECTOR_ROLE = "nestor"        # owns the schema, the only role that writes
READER_ROLE = "hermes_reader"    # Hermes MCP server: SELECT on reader.* views only


def with_database(url: str, database: str, user: str | None = None, password: str | None = None) -> str:
    """Same server as url, another database (and optionally another user)."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        netloc = f"{user}:{password}@{netloc.rsplit('@', 1)[-1]}"
    return urlunsplit(parts._replace(netloc=netloc, path=f"/{database}"))


async def _ensure_role(conn: asyncpg.Connection, role: str, password: str) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    verb = "ALTER" if exists else "CREATE"
    # DDL takes no bind parameters; format() quotes the identifier and the literal.
    sql = await conn.fetchval(f"SELECT format('{verb} ROLE %I LOGIN PASSWORD %L', $1::text, $2::text)", role, password)
    await conn.execute(sql)


async def bootstrap(admin_url: str, database: str, collector_password: str, reader_password: str) -> None:
    admin = await asyncpg.connect(admin_url)
    try:
        await _ensure_role(admin, COLLECTOR_ROLE, collector_password)
        await _ensure_role(admin, READER_ROLE, reader_password)
        # Second line of defence: every transaction of the reader is read-only.
        await admin.execute(f"ALTER ROLE {READER_ROLE} SET default_transaction_read_only = on")
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
            await admin.execute(
                f'CREATE DATABASE "{database}" OWNER {COLLECTOR_ROLE} ENCODING \'UTF8\' TEMPLATE template0'
            )
        await admin.execute(f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC')
        # CREATE is needed for the reader schema when the database already existed
        # with another owner (e.g. created by the postgres Docker image).
        await admin.execute(f'GRANT CONNECT, CREATE, TEMPORARY ON DATABASE "{database}" TO {COLLECTOR_ROLE}')
        await admin.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {READER_ROLE}')
    finally:
        await admin.close()

    db = await asyncpg.connect(with_database(admin_url, database))
    try:
        # PostgreSQL 15+ already denies CREATE on public; also hide its tables from other roles.
        await db.execute(f"ALTER SCHEMA public OWNER TO {COLLECTOR_ROLE}")
        await db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    finally:
        await db.close()


async def _main() -> None:
    from dotenv import load_dotenv

    from .logs import setup_logging

    load_dotenv()
    setup_logging()
    try:
        admin_url = os.environ["PG_ADMIN_URL"]
        collector_password = os.environ["COLLECTOR_PASSWORD"]
        reader_password = os.environ["READER_PASSWORD"]
    except KeyError as e:
        raise SystemExit(f"missing required environment variable {e.args[0]}") from None
    database = os.environ.get("NESTOR_DB", "nestor")
    await bootstrap(admin_url, database, collector_password, reader_password)
    log.info("bootstrap done", extra={"database": database})


if __name__ == "__main__":
    asyncio.run(_main())
