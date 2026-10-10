"""The charts' snapshots and what is known about the artists in them (migration 0009)."""
from __future__ import annotations

from datetime import date, datetime

import asyncpg

from .sources import Entry, artist_key, main_artist

KEEP_DAYS = 120         # snapshots older than this go; the scores look back four weeks


async def states(conn: asyncpg.Connection) -> dict[str, asyncpg.Record]:
    return {r["key"]: r for r in await conn.fetch("SELECT * FROM music_sources")}


async def save_run(conn, key: str, now: datetime, *, status: str, day: date | None = None, size: int | None = None,
                   next_run_at: datetime | None = None) -> None:
    """day: the local day of a snapshot just saved; None: a failed read, failures grow."""
    ok = day is not None
    await conn.execute(
        """INSERT INTO music_sources (key, last_run_at, last_ok_day, next_run_at, status, size, failures)
           VALUES ($1, $2, $3, $4, $5, $6, CASE WHEN $7 THEN 0 ELSE 1 END)
           ON CONFLICT (key) DO UPDATE
             SET last_run_at = $2, last_ok_day = coalesce($3, music_sources.last_ok_day), next_run_at = $4,
                 status = $5, size = coalesce($6, music_sources.size),
                 failures = CASE WHEN $7 THEN 0 ELSE music_sources.failures + 1 END""",
        key, now, day, next_run_at, status, size, ok)


async def save_chart(conn, day: date, source: str, entries: list[Entry]) -> None:
    """The chart of the day, replacing an earlier read of the same day."""
    async with conn.transaction():
        await conn.execute("DELETE FROM music_snapshots WHERE day = $1 AND source = $2", day, source)
        seen: set[int] = set()
        for e in entries:
            if e.position in seen:
                continue
            seen.add(e.position)
            key = artist_key(e.artist)
            if not key:
                continue
            await conn.execute(
                """INSERT INTO music_snapshots (day, source, position, artist, artist_key, title, shift, is_new, url)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                day, source, e.position, e.artist, key, e.title, e.shift, e.is_new, e.url)
            await conn.execute("INSERT INTO music_artists (artist_key, name) VALUES ($1, $2) "
                               "ON CONFLICT (artist_key) DO NOTHING", key, main_artist(e.artist))


async def history(conn, since: date) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        "SELECT day, source, position, artist, artist_key, title, shift, is_new, url FROM music_snapshots "
        "WHERE day >= $1 ORDER BY day, source, position", since))


async def artists(conn, keys: list[str]) -> dict[str, asyncpg.Record]:
    rows = await conn.fetch("SELECT * FROM music_artists WHERE artist_key = ANY($1::text[])", keys)
    return {r["artist_key"]: r for r in rows}


async def set_listeners(conn, key: str, listeners: int | None, now: datetime) -> None:
    await conn.execute("UPDATE music_artists SET listeners = $2, listeners_at = $3 WHERE artist_key = $1",
                       key, listeners, now)


async def set_verdict(conn, key: str, *, known: bool, russian: bool, now: datetime) -> None:
    """The check model's verdict; an artist marked known by /music known stays known."""
    await conn.execute(
        """UPDATE music_artists SET russian = $3, checked_at = $4,
                  known = known OR $2, known_reason = CASE WHEN known THEN known_reason WHEN $2 THEN 'model' END
           WHERE artist_key = $1""", key, known, russian, now)


async def mark_known(conn, name: str) -> str | None:
    """/music known <name>: never suggest this artist. Returns the name as stored, None if the name is empty."""
    key = artist_key(name)
    if not key:
        return None
    return await conn.fetchval(
        """INSERT INTO music_artists (artist_key, name, known, known_reason) VALUES ($1, $2, TRUE, 'command')
           ON CONFLICT (artist_key) DO UPDATE SET known = TRUE, known_reason = 'command'
           RETURNING name""", key, main_artist(name))


async def mark_recommended(conn, keys: list[str], now: datetime) -> None:
    await conn.execute("UPDATE music_artists SET recommended_at = $2 WHERE artist_key = ANY($1::text[])", keys, now)


async def cleanup(conn, today: date) -> None:
    await conn.execute("DELETE FROM music_snapshots WHERE day < $1", today.fromordinal(today.toordinal() - KEEP_DAYS))
