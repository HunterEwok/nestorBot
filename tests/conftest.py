"""Database fixtures.

Set NESTOR_TEST_PG_ADMIN_URL (environment or .env) to a superuser DSN of a THROWAWAY
cluster, e.g. the db-test service from docker-compose.yml:
  postgresql://postgres:postgres@localhost:5433/postgres
The roles nestor, hermes_reader and nestor_backup and the database nestor_test are dropped and
recreated. Without the variable, database tests are skipped.
"""
from __future__ import annotations

import os

import asyncpg
import pytest
from dotenv import load_dotenv

from nestor.bootstrap import bootstrap, ensure_backup_role, with_database
from nestor.collector import store
from nestor.migrate import migrate

load_dotenv()
ADMIN_URL = os.environ.get("NESTOR_TEST_PG_ADMIN_URL")
DB = "nestor_test"
# Databases some tests make beside it. One left behind by an interrupted run would keep the roles
# from being dropped (their grants and default privileges live in it), so they go first.
SCRATCH_DBS = ("nestor_preexisting", "nestor_bootstrap_test", "nestor_restored_test", "nestor_older_test")


def _dsn(user: str, password: str) -> str:
    return with_database(ADMIN_URL, DB, user, password)


@pytest.fixture(scope="session")
async def database():
    if not ADMIN_URL:
        pytest.skip("NESTOR_TEST_PG_ADMIN_URL not set")
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    for name in SCRATCH_DBS:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin.execute("DROP ROLE IF EXISTS nestor_backup")
    await admin.execute("DROP ROLE IF EXISTS hermes_reader")
    await admin.execute("DROP ROLE IF EXISTS nestor")
    await admin.close()
    await bootstrap(ADMIN_URL, DB, "collector", "reader")
    await ensure_backup_role(ADMIN_URL, DB, "backup")       # before the migrations: the default privileges reach their tables
    owner = await asyncpg.connect(_dsn("nestor", "collector"))
    await migrate(owner)
    await owner.close()
    yield {"collector": _dsn("nestor", "collector"), "reader": _dsn("hermes_reader", "reader"),
           "backup": _dsn("nestor_backup", "backup")}


@pytest.fixture
async def pool(database):
    pool = await store.create_pool(database["collector"])
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE chats, users, messages, message_edits, bot_requests, reminders, daily_posts, "
                           "request_tickets, news_sources, news_items, news_digests, maintenance_runs, "
                           "retention_marks, ops_state CASCADE")
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


@pytest.fixture
async def backup_role(database, pool):
    c = await asyncpg.connect(database["backup"])
    await store.init_connection(c)
    yield c
    await c.close()
