"""One-time database setup, run with superuser rights: roles and the database.

Tables come from migrations (python -m nestor.migrate). Safe to re-run: existing
roles get their passwords updated, an existing database is kept.

Usage: python -m nestor.bootstrap   (reads PG_ADMIN_URL, COLLECTOR_PASSWORD, READER_PASSWORD
                                     and optionally NESTOR_DB from the environment or .env;
                                     with BACKUP_PASSWORD set it also ensures the backup role)
       python -m nestor.bootstrap backup   (only the backup role: PG_ADMIN_URL, BACKUP_PASSWORD, NESTOR_DB;
                                            the passwords of the other roles stay as they are)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import stringprep
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import asyncpg

log = logging.getLogger(__name__)

COLLECTOR_ROLE = "nestor"        # owns the schema, the only role that writes
READER_ROLE = "hermes_reader"    # Hermes MCP server: SELECT on reader.* views only
BACKUP_ROLE = "nestor_backup"    # nightly pg_dump (nestor.backup): SELECT on the whole database, nothing else


def with_database(url: str, database: str, user: str | None = None, password: str | None = None) -> str:
    """Same server as url, another database (and optionally another user)."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        netloc = f"{user}:{password}@{netloc.rsplit('@', 1)[-1]}"
    return urlunsplit(parts._replace(netloc=netloc, path=f"/{database}"))


# SASLprep's prohibited output (RFC 4013 2.3) and the unassigned code points PostgreSQL refuses with it.
_PROHIBITED = (stringprep.in_table_a1, stringprep.in_table_c12, stringprep.in_table_c21_c22, stringprep.in_table_c3,
               stringprep.in_table_c4, stringprep.in_table_c5, stringprep.in_table_c6, stringprep.in_table_c7,
               stringprep.in_table_c8, stringprep.in_table_c9)


def _saslprep(password: str) -> str:
    """The password as PostgreSQL's pg_saslprep (and libpq, and asyncpg) hashes it: other spaces become a
    space, invisible characters go, NFKC. A password SASLprep refuses (prohibited or unassigned
    characters, mixed directions) is hashed as it is, as they do. ASCII, as the generated passwords
    are, comes back unchanged."""
    if password.isascii():
        return password
    mapped = "".join(" " if stringprep.in_table_c12(c) else c for c in password if not stringprep.in_table_b1(c))
    prepared = unicodedata.normalize("NFKC", mapped)
    if not prepared or any(table(c) for c in prepared for table in _PROHIBITED):
        return password
    if any(stringprep.in_table_d1(c) for c in prepared) and (
            any(stringprep.in_table_d2(c) for c in prepared)
            or not (stringprep.in_table_d1(prepared[0]) and stringprep.in_table_d1(prepared[-1]))):
        return password
    return prepared


def scram_verifier(password: str, *, iterations: int = 4096, salt: bytes | None = None) -> str:
    """What PostgreSQL stores for a SCRAM-SHA-256 password, computed here: 'SCRAM-SHA-256$4096:<salt>$
    <StoredKey>:<ServerKey>' (RFC 5802, 7677). Sent instead of the password, which then never reaches the
    server's statement log or its memory of bind parameters."""
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", _saslprep(password).encode(), salt, iterations)
    stored_key = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")
    salt_b64, stored_b64, server_b64 = (base64.b64encode(raw).decode() for raw in (salt, stored_key, server_key))
    return f"SCRAM-SHA-256${iterations}:{salt_b64}${stored_b64}:{server_b64}"


async def _ensure_role(conn: asyncpg.Connection, role: str, password: str) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    verb = "ALTER" if exists else "CREATE"
    # DDL takes no bind parameters; format() quotes the identifier and the literal. The literal is the
    # SCRAM verifier, never the password: the server stores a verifier as it is.
    sql = await conn.fetchval(f"SELECT format('{verb} ROLE %I LOGIN PASSWORD %L', $1::text, $2::text)", role,
                              scram_verifier(password))
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


async def ensure_backup_role(admin_url: str, database: str, password: str) -> None:
    """The role of the nightly dump: it reads every table of `database`, present and future, and cannot
    write. Not pg_read_all_data, which would reach every database of the server. Needs the role nestor
    (bootstrap) and is safe to re-run."""
    admin = await asyncpg.connect(admin_url)
    try:
        await _ensure_role(admin, BACKUP_ROLE, password)
        await admin.execute(f"ALTER ROLE {BACKUP_ROLE} CONNECTION LIMIT 3")
        await admin.execute(f"ALTER ROLE {BACKUP_ROLE} SET default_transaction_read_only = on")
        await admin.execute(f"REVOKE pg_read_all_data FROM {BACKUP_ROLE}")     # in case an earlier setup granted it
        await admin.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {BACKUP_ROLE}')
    finally:
        await admin.close()

    db = await asyncpg.connect(with_database(admin_url, database))
    try:
        await db.execute(f"GRANT USAGE ON SCHEMA public TO {BACKUP_ROLE}")
        await db.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {BACKUP_ROLE}")
        await db.execute(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {BACKUP_ROLE}")
        # Tables and sequences that later migrations create (they run as the owner, nestor).
        await db.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE {COLLECTOR_ROLE} IN SCHEMA public "
                         f"GRANT SELECT ON TABLES TO {BACKUP_ROLE}")
        await db.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE {COLLECTOR_ROLE} IN SCHEMA public "
                         f"GRANT SELECT ON SEQUENCES TO {BACKUP_ROLE}")
        if await db.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = 'reader'"):
            await db.execute(f"GRANT USAGE ON SCHEMA reader TO {BACKUP_ROLE}")
    finally:
        await db.close()


async def _main(argv: list[str] | None = None) -> None:
    import argparse

    from dotenv import load_dotenv

    from .logs import setup_logging

    parser = argparse.ArgumentParser(prog="python -m nestor.bootstrap", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", nargs="?", choices=["backup"],
                        help="backup: only the backup role, nothing else is touched")
    args = parser.parse_args(argv)
    load_dotenv()
    setup_logging()

    def require(name: str) -> str:
        try:
            return os.environ[name]
        except KeyError:
            raise SystemExit(f"missing required environment variable {name}") from None

    admin_url = require("PG_ADMIN_URL")
    database = os.environ.get("NESTOR_DB", "nestor")
    where = {"server": urlsplit(admin_url).hostname, "database": database}     # never the password
    if args.command == "backup":
        if not require("BACKUP_PASSWORD"):      # .env.example leaves it empty: a role without a password is no role
            raise SystemExit("BACKUP_PASSWORD is empty")
        await ensure_backup_role(admin_url, database, os.environ["BACKUP_PASSWORD"])
        log.info("backup role ready", extra=where)
        return
    await bootstrap(admin_url, database, require("COLLECTOR_PASSWORD"), require("READER_PASSWORD"))
    if os.environ.get("BACKUP_PASSWORD"):
        await ensure_backup_role(admin_url, database, os.environ["BACKUP_PASSWORD"])
        log.info("backup role ready", extra=where)
    log.info("bootstrap done", extra=where)


if __name__ == "__main__":
    asyncio.run(_main())
