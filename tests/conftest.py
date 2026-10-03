"""Database fixtures.

Set NESTOR_TEST_PG_ADMIN_URL to a superuser DSN of a THROWAWAY cluster, e.g.
  postgresql://postgres@/postgres?host=/var/run/postgresql
The roles nestor and hermes_reader and the database nestor_test are dropped and recreated.
Without the variable, database tests are skipped.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from nestor.collector import store
from nestor.migrate import migrate

ADMIN_URL = os.environ.get("NESTOR_TEST_PG_ADMIN_URL")
DB = "nestor_test"
ROOT = Path(__file__).resolve().parent.parent

requires_db = pytest.mark.skipif(not ADMIN_URL, reason="NESTOR_TEST_PG_ADMIN_URL not set")


def _dsn(user: str, password: str) -> str:
    """Admin DSN with another user and the test database."""
    parts = urlsplit(ADMIN_URL)
    hostport = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{hostport}", path=f"/{DB}"))


@pytest.fixture(scope="session")
async def database():
    if not ADMIN_URL:
        pytest.skip("NESTOR_TEST_PG_ADMIN_URL not set")
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    await admin.execute("DROP ROLE IF EXISTS hermes_reader")
    await admin.execute("DROP ROLE IF EXISTS nestor")
    await admin.close()
    subprocess.run(
        ["psql", ADMIN_URL, "-q", "-v", f"db={DB}", "-v", "collector_password=collector",
         "-v", "reader_password=reader", "-f", str(ROOT / "deploy/postgres/bootstrap.sql")],
        check=True, capture_output=True,
    )
    owner = await asyncpg.connect(_dsn("nestor", "collector"))
    await migrate(owner)
    await owner.close()
    yield {"collector": _dsn("nestor", "collector"), "reader": _dsn("hermes_reader", "reader")}


@pytest.fixture
async def pool(database):
    pool = await store.create_pool(database["collector"])
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE chats, users, messages, message_edits, bot_requests CASCADE")
    yield pool
    await pool.close()


@pytest.fixture
async def conn(pool):
    async with pool.acquire() as c:
        yield c


@pytest.fixture
async def reader(database, pool):
    c = await asyncpg.connect(database["reader"])
    await store.init_connection(c)
    yield c
    await c.close()
