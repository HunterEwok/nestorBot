"""Stage 1 acceptance: hermes_reader can read the reader views and nothing else."""
import asyncpg
import pytest

from .test_store import save
from .factories import message

FORBIDDEN = [
    "INSERT INTO public.messages (chat_id, message_id, sent_at, raw) VALUES (1, 1, now(), '{}')",
    "INSERT INTO reader.messages (chat_id, message_id) VALUES (1, 1)",
    "UPDATE reader.users SET username = 'x'",
    "DELETE FROM reader.messages",
    "SELECT raw FROM public.messages",
    "SELECT * FROM public.bot_requests",
    "SELECT * FROM public.users",
    "CREATE TABLE reader.evil (x int)",
    "CREATE TABLE evil (x int)",
    "DROP VIEW reader.messages",
    "TRUNCATE public.messages",
]


@pytest.mark.parametrize("sql", FORBIDDEN)
async def test_reader_cannot_write_or_see_tables(reader, sql):
    with pytest.raises(asyncpg.PostgresError) as e:
        await reader.execute(sql)
    assert isinstance(e.value, (
        asyncpg.InsufficientPrivilegeError,
        asyncpg.ReadOnlySQLTransactionError,
        asyncpg.ObjectNotInPrerequisiteStateError,  # "cannot insert into view"
    ))


async def test_reader_sessions_are_read_only(reader):
    assert await reader.fetchval("SHOW default_transaction_read_only") == "on"


async def test_reader_sees_messages_without_raw_or_opted_out(conn, reader):
    await save(conn, message(7, "тезисы о погоде", uid=111))
    await save(conn, message(8, "не храните меня", uid=222))
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 222")

    rows = await reader.fetch("SELECT * FROM reader.messages")
    assert [r["message_id"] for r in rows] == [7]
    assert "raw" not in rows[0].keys()
    assert rows[0]["link"] == "https://t.me/c/1000000001/7"
    assert rows[0]["author_name"] == "User111"
    assert [r["user_id"] for r in await reader.fetch("SELECT user_id FROM reader.users")] == [111]


async def test_russian_full_text_search(conn, reader):
    await save(conn, message(1, "Петя выдвинул три тезиса"))
    await save(conn, message(2, "погода хорошая"))
    found = await reader.fetch(
        "SELECT message_id FROM reader.messages WHERE tsv @@ plainto_tsquery('russian', 'тезисы')"
    )
    assert [r["message_id"] for r in found] == [1]


async def test_forum_topic_link(conn, reader):
    await save(conn, message(11, "в теме", message_thread_id=10, is_topic_message=True))
    assert await reader.fetchval("SELECT link FROM reader.messages") == "https://t.me/c/1000000001/10/11"


async def test_bootstrap_can_be_rerun(database, reader):
    from nestor.bootstrap import bootstrap

    from .conftest import ADMIN_URL, DB

    await bootstrap(ADMIN_URL, DB, "collector", "reader")
    assert await reader.fetchval("SELECT count(*) FROM reader.chats") == 0


async def test_bootstrap_into_existing_database_owned_by_someone_else(database):
    """As with the postgres Docker image: the database exists, owned by the superuser."""
    import os
    import subprocess
    import sys

    import asyncpg as pg

    from nestor.bootstrap import bootstrap, with_database

    from .conftest import ADMIN_URL

    admin = await pg.connect(ADMIN_URL)
    await admin.execute("DROP DATABASE IF EXISTS nestor_preexisting")
    await admin.execute("CREATE DATABASE nestor_preexisting")
    await admin.close()
    try:
        await bootstrap(ADMIN_URL, "nestor_preexisting", "collector", "reader")
        env = {k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN",)}
        env["DATABASE_URL"] = with_database(ADMIN_URL, "nestor_preexisting", "nestor", "collector")
        # Also: the migration CLI needs nothing but DATABASE_URL.
        done = subprocess.run([sys.executable, "-m", "nestor.migrate"], env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert "0002_reader_views" in done.stdout
    finally:
        admin = await pg.connect(ADMIN_URL)
        await admin.execute("DROP DATABASE IF EXISTS nestor_preexisting")
        await admin.close()
