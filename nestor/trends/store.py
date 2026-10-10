"""The charts' snapshots, the artists in them and the songs of the day (migrations 0009, 0010)."""
from __future__ import annotations

from datetime import date, datetime

import asyncpg

from .sources import Entry, artist_key, main_artist
from .viral import Candidate, Pick

KEEP_DAYS = 200         # snapshots older than this go: half a year of history for --backtest


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


async def save_chart(conn, day: date, source: str, entries: list[Entry], origin: str = "read") -> None:
    """The chart of the day, replacing an earlier read of the same day. origin 'wayback': a historical
    edition, never replacing one of our own reads."""
    async with conn.transaction():
        if origin != "read" and await conn.fetchval(
                "SELECT 1 FROM music_snapshots WHERE day = $1 AND source = $2 AND origin = 'read' LIMIT 1", day, source):
            return
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
                """INSERT INTO music_snapshots (day, source, position, artist, artist_key, title, shift, is_new, url,
                                               origin)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                day, source, e.position, e.artist, key, e.title, e.shift, e.is_new, e.url, origin)
            await conn.execute("INSERT INTO music_artists (artist_key, name) VALUES ($1, $2) "
                               "ON CONFLICT (artist_key) DO NOTHING", key, main_artist(e.artist))


async def history(conn, since: date) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        "SELECT day, source, position, artist, artist_key, title, shift, is_new, url FROM music_snapshots "
        "WHERE day >= $1 ORDER BY day, source, position", since))


async def days(conn, source: str, since: date) -> set[date]:
    return {r["day"] for r in await conn.fetch(
        "SELECT DISTINCT day FROM music_snapshots WHERE source = $1 AND day >= $2", source, since)}


async def picks(conn, since: date) -> list[Pick]:
    rows = await conn.fetch("SELECT day, song_key, artist_key FROM music_picks WHERE day >= $1", since)
    return [Pick(r["day"], r["song_key"], r["artist_key"]) for r in rows]


async def save_pick(conn, day: date, c: Candidate) -> None:
    await conn.execute(
        """INSERT INTO music_picks (day, song_key, artist_key, artist, title, score) VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (day) DO NOTHING""", day, c.key, c.artist_key, c.artist, c.title, c.score)


async def banned(conn) -> frozenset[str]:
    """Artists marked by /music known: never the song of the day."""
    return frozenset(r["artist_key"] for r in await conn.fetch(
        "SELECT artist_key FROM music_artists WHERE known AND known_reason = 'command'"))


async def artists(conn, keys: list[str]) -> dict[str, asyncpg.Record]:
    rows = await conn.fetch("SELECT * FROM music_artists WHERE artist_key = ANY($1::text[])", keys)
    return {r["artist_key"]: r for r in rows}


async def mark_known(conn, name: str) -> str | None:
    """/music known <name>: never pick this artist's songs. Returns the name as stored, None if the name is empty."""
    key = artist_key(name)
    if not key:
        return None
    return await conn.fetchval(
        """INSERT INTO music_artists (artist_key, name, known, known_reason) VALUES ($1, $2, TRUE, 'command')
           ON CONFLICT (artist_key) DO UPDATE SET known = TRUE, known_reason = 'command'
           RETURNING name""", key, main_artist(name))


async def post_of(conn, day: date) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT html, plain, preview_url, keyboard FROM music_posts WHERE day = $1", day)


async def save_post(conn, day: date, html: str, plain: str, preview_url: str | None, keyboard: dict | None) -> None:
    await conn.execute(
        """INSERT INTO music_posts (day, html, plain, preview_url, keyboard) VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (day) DO NOTHING""", day, html, plain, preview_url, keyboard)


async def cleanup(conn, today: date) -> None:
    before = today.fromordinal(today.toordinal() - KEEP_DAYS)
    await conn.execute("DELETE FROM music_snapshots WHERE day < $1", before)
    await conn.execute("DELETE FROM music_posts WHERE day < $1", before)
    await conn.execute("DELETE FROM music_picks WHERE day < $1", before)
