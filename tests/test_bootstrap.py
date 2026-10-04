"""Roles and databases made by python -m nestor.bootstrap: the passwords reach the server only as SCRAM verifiers,
and the backup role is its own step."""
import os
import subprocess
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import asyncpg
import pytest

from nestor import bootstrap

from .conftest import ADMIN_URL, DB

SCRATCH_ROLE = "nestor_verifier_test"
SCRATCH_DB = "nestor_bootstrap_test"


class Recorder(asyncpg.Connection):
    """An admin connection that remembers every statement and parameter it sent."""

    sent: ClassVar[list] = []

    async def fetchval(self, query, *args, **kwargs):
        self.sent.append((query, args))
        return await super().fetchval(query, *args, **kwargs)

    async def execute(self, query, *args, **kwargs):
        self.sent.append((query, args))
        return await super().execute(query, *args, **kwargs)


def _psql() -> Path | None:
    """libpq's own client, as pg_dump logs in: the newest installed."""
    found = sorted(Path("/usr/lib/postgresql").glob("[0-9]*/bin/psql"), key=lambda p: int(p.parent.parent.name))
    return found[-1] if found else None


def _libpq_logs_in(psql: Path, url: str, password: str) -> bool:
    done = subprocess.run([str(psql), "-w", "-X", "-Atc", "SELECT current_user", url], capture_output=True, text=True,
                          env={**os.environ, "PGPASSWORD": password}, check=False)
    return done.returncode == 0 and done.stdout.strip() == SCRATCH_ROLE


async def test_role_password_set_as_verifier_logs_in(database):
    Recorder.sent.clear()
    host = urlsplit(ADMIN_URL)
    url = f"postgresql://{SCRATCH_ROLE}@{host.hostname}:{host.port}/postgres"       # any database: PUBLIC may connect
    psql = _psql()
    admin = await asyncpg.connect(ADMIN_URL, connection_class=Recorder)
    try:
        await admin.execute(f"DROP ROLE IF EXISTS {SCRATCH_ROLE}")
        # password: (also logs in, does not). A decomposed ä: PostgreSQL and the client both compose it, so the
        # verifier must be of the composed one. With a left-to-right mark, or Hebrew next to Latin, SASLprep
        # refuses the password, and both sides hash it as it is: then the composed ä is another password.
        cases = {
            "s3kr1t-ASCII": ((), ("s3kr1t-ascii",)),
            "pa\u0308ss-Пароль": (("p\u00e4ss-Пароль",), ()),
            "pa\u0308ss\u200e-Пароль": ((), ("p\u00e4ss\u200e-Пароль", "pa\u0308ss-Пароль")),
            "\u05e4\u05e1-pa\u0308ss-Пароль": ((), ("\u05e4\u05e1-p\u00e4ss-Пароль",)),
        }
        for password, (accepted, refused) in cases.items():
            Recorder.sent.clear()
            await bootstrap._ensure_role(admin, SCRATCH_ROLE, password)       # created, then altered: both verbs
            await bootstrap._ensure_role(admin, SCRATCH_ROLE, password)
            assert "s3kr1t" not in repr(Recorder.sent) and "Пароль" not in repr(Recorder.sent)     # never sent
            stored = await admin.fetchval("SELECT rolpassword FROM pg_authid WHERE rolname = $1", SCRATCH_ROLE)
            assert stored.startswith("SCRAM-SHA-256$4096:") and password not in stored
            for typed in (password, *accepted):
                login = await asyncpg.connect(url, password=typed)
                assert await login.fetchval("SELECT current_user") == SCRATCH_ROLE
                await login.close()
            for typed in (password + "!", *refused):
                with pytest.raises(asyncpg.InvalidPasswordError):
                    await asyncpg.connect(url, password=typed)
            if psql:                                                           # and libpq agrees, as pg_dump uses it
                assert all(_libpq_logs_in(psql, url, typed) for typed in (password, *accepted))
                assert not any(_libpq_logs_in(psql, url, typed) for typed in (password + "!", *refused))
    finally:
        await admin.execute(f"DROP ROLE IF EXISTS {SCRATCH_ROLE}")
        await admin.close()


async def test_the_backup_role_is_made_again_with_a_new_password_and_the_old_one_stops_working(database):
    from nestor.bootstrap import ensure_backup_role, with_database

    try:
        await ensure_backup_role(ADMIN_URL, DB, "other-password")
        with pytest.raises(asyncpg.InvalidPasswordError):
            await asyncpg.connect(database["backup"])
        login = await asyncpg.connect(with_database(ADMIN_URL, DB, "nestor_backup", "other-password"))
        assert await login.fetchval("SELECT count(*) FROM public.messages") >= 0
        await login.close()
    finally:
        await ensure_backup_role(ADMIN_URL, DB, "backup")
    login = await asyncpg.connect(database["backup"])
    await login.close()


async def _scratch_cleanup():
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    finally:
        await admin.close()


@pytest.fixture
def cli_env(database, monkeypatch):
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level=None: None)        # keeps pytest's log capture
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)              # a developer's .env stays out
    monkeypatch.setenv("PG_ADMIN_URL", ADMIN_URL)
    monkeypatch.setenv("COLLECTOR_PASSWORD", "collector")
    monkeypatch.setenv("READER_PASSWORD", "reader")
    monkeypatch.setenv("NESTOR_DB", SCRATCH_DB)
    monkeypatch.delenv("BACKUP_PASSWORD", raising=False)


async def test_the_backup_command_does_only_the_backup_role(database, cli_env, monkeypatch, caplog):
    import logging

    from nestor.bootstrap import with_database

    await _scratch_cleanup()
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f"CREATE DATABASE {SCRATCH_DB} OWNER nestor")       # what `bootstrap` made earlier
        monkeypatch.setenv("BACKUP_PASSWORD", "backup")
        monkeypatch.setenv("COLLECTOR_PASSWORD", "must-not-be-applied")           # `backup` never reads these two
        monkeypatch.delenv("READER_PASSWORD")
        with caplog.at_level(logging.INFO, logger="nestor.bootstrap"):
            await bootstrap._main(["backup"])
        assert [r.getMessage() for r in caplog.records] == ["backup role ready"]
        login = await asyncpg.connect(with_database(ADMIN_URL, SCRATCH_DB, "nestor_backup", "backup"))
        await login.close()
        login = await asyncpg.connect(with_database(ADMIN_URL, SCRATCH_DB, "nestor", "collector"))   # unchanged
        await login.close()
        monkeypatch.setenv("BACKUP_PASSWORD", "")
        with pytest.raises(SystemExit, match="BACKUP_PASSWORD is empty"):
            await bootstrap._main(["backup"])
        monkeypatch.delenv("BACKUP_PASSWORD")
        with pytest.raises(SystemExit, match="missing required environment variable BACKUP_PASSWORD"):
            await bootstrap._main(["backup"])
    finally:
        await admin.close()
        await _scratch_cleanup()


async def test_plain_bootstrap_makes_the_backup_role_only_when_its_password_is_set(database, cli_env, monkeypatch):
    from nestor.bootstrap import with_database
    from nestor.migrate import migrate

    await _scratch_cleanup()
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await bootstrap._main([])                                              # no BACKUP_PASSWORD: the role is not touched
        assert await admin.fetchval(
            "SELECT has_database_privilege('nestor_backup', $1, 'CONNECT')", SCRATCH_DB) is False
        # As in production: the archive is migrated and full long before the backup role comes.
        owner = await asyncpg.connect(with_database(ADMIN_URL, SCRATCH_DB, "nestor", "collector"))
        try:
            await migrate(owner)
            await owner.execute("INSERT INTO chats (chat_id, type) VALUES (-100, 'supergroup')")
        finally:
            await owner.close()
        monkeypatch.setenv("BACKUP_PASSWORD", "backup")
        await bootstrap._main([])
        assert await admin.fetchval(
            "SELECT has_database_privilege('nestor_backup', $1, 'CONNECT')", SCRATCH_DB) is True
        login = await asyncpg.connect(with_database(ADMIN_URL, SCRATCH_DB, "nestor_backup", "backup"))
        try:
            assert await login.fetchval("SHOW default_transaction_read_only") == "on"
            # The tables that were there before the role: every one of them, their sequences, the reader schema.
            for table in await login.fetchval(
                    "SELECT array_agg(tablename) FROM pg_tables WHERE schemaname = 'public'"):
                await login.fetchval(f"SELECT count(*) FROM public.{table}")
            assert await login.fetchval("SELECT count(*) FROM public.chats") == 1
            assert await login.fetchval("SELECT last_value FROM public.bot_requests_id_seq") == 1
            assert await login.fetchval("SELECT has_schema_privilege('reader', 'USAGE')") is True
        finally:
            await login.close()
    finally:
        await admin.close()
        await _scratch_cleanup()
