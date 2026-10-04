"""news_items and news_sources: saving fetched items, gluing one story from several sources
together, the triage queue, candidates for the digest, retention."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta

import asyncpg

from .model import Item, Source

CONTENT_DAYS = 30        # then only the identifiers stay, so old feed entries are not taken for new
MARKER_DAYS = 400
STORY_DAYS = 7           # how far back an item may join an earlier story
MIN_TITLE_KEY = 25       # shorter titles ("News", "Update") never glue stories together
TRIAGE_DAYS = 2          # items that waited longer (Hermes down for days) are too old for a digest anyway


def title_key(title: str) -> str | None:
    text = unicodedata.normalize("NFKC", title or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:160]
    return text if len(text) >= MIN_TITLE_KEY else None


def _clean(value):
    """Text Postgres accepts: no NUL, and a lone surrogate (a cut JSON escape) cannot even be
    encoded, so it becomes U+FFFD. Lists and dicts are cleaned all the way down."""
    if isinstance(value, str):
        return value.replace("\x00", "").encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    return value


def _plain(value: dict) -> dict:
    """JSON-safe copy (the pool's jsonb codec has no fallback for dates and the like)."""
    return _clean(json.loads(json.dumps(value, default=str)))


async def states(conn) -> dict[str, asyncpg.Record]:
    return {r["key"]: r for r in await conn.fetch("SELECT * FROM news_sources")}


async def save_run(conn, key: str, now: datetime, *, ok: bool, status: str, cursor: dict | None = None,
                   newest: datetime | None = None, next_run_at: datetime | None = None, seeded: bool = False) -> None:
    # status may quote a server's error message
    await conn.execute(
        """INSERT INTO news_sources AS s (key, last_run_at, last_ok_at, next_run_at, newest_item_at, cursor, seeded,
                                          status, failures)
           VALUES ($1, $2::timestamptz, CASE WHEN $3::bool THEN $2::timestamptz END, $4::timestamptz, $5::timestamptz,
                   coalesce($6::jsonb, '{}'), $7::bool, $8, CASE WHEN $3::bool THEN 0 ELSE 1 END)
           ON CONFLICT (key) DO UPDATE SET
             last_run_at = $2::timestamptz,
             last_ok_at = CASE WHEN $3::bool THEN $2::timestamptz ELSE s.last_ok_at END,
             next_run_at = $4::timestamptz,
             newest_item_at = greatest(s.newest_item_at, $5::timestamptz),
             cursor = coalesce($6::jsonb, s.cursor),
             seeded = s.seeded OR $7::bool,
             status = $8,
             failures = CASE WHEN $3::bool THEN 0 ELSE s.failures + 1 END""",
        key, now, ok, next_run_at, newest, _plain(cursor) if cursor is not None else None, seeded, _clean(status))


async def save_item(conn, source: Source, item: Item, status: str, reason: str | None = None) -> int | None:
    """Insert a new item; None if this source already had it."""
    title = _clean(item.title)
    return await conn.fetchval(
        """INSERT INTO news_items (source, ext_id, url, title, summary, doi, title_key, lang, categories, meta,
                                   published_at, status, drop_reason, section)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
           ON CONFLICT (source, ext_id) DO NOTHING RETURNING id""",
        source.key, _clean(item.ext_id)[:500], _clean(item.url), title, _clean(item.summary) or None,
        _clean(item.doi), title_key(title), _clean(item.lang or source.lang) or None, _clean(list(item.categories)),
        _plain(item.meta), item.published_at, status, reason, source.section)


async def link_stories(conn) -> int:
    """Glue new items to an earlier story with the same DOI, page or title. Runs after every
    source of a pass has saved its items, in id order, so the first report starts the story.
    A preprint also joins its journal version: bioRxiv names the journal DOI, Europe PMC the PMID."""
    rows = await conn.fetch(
        """SELECT id, doi, url, title_key, meta->>'published_doi' AS published_doi,
                  meta->>'published_pmid' AS published_pmid,
                  CASE WHEN source = 'pubmed' THEN ext_id END AS pmid FROM news_items
           WHERE story_id IS NULL AND status NOT IN ('seen', 'dropped') ORDER BY id""")
    for r in rows:
        story = await conn.fetchval(
            """SELECT coalesce(story_id, id) FROM news_items
               WHERE id < $1 AND status NOT IN ('seen', 'dropped') AND fetched_at > now() - make_interval(days => $5)
                 AND (doi = $2 OR url = $3 OR title_key = $4 OR doi = $6 OR meta->>'published_doi' = $2
                      OR (source = 'pubmed' AND ext_id = $7) OR meta->>'published_pmid' = $8)
               ORDER BY id LIMIT 1""",
            r["id"], r["doi"], r["url"], r["title_key"], STORY_DAYS, r["published_doi"], r["published_pmid"], r["pmid"])
        await conn.execute("UPDATE news_items SET story_id = $2 WHERE id = $1", r["id"], story or r["id"])
    return len(rows)


# ---- triage ----

async def pending(conn, limit: int) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        """SELECT id, source, url, title, summary, lang, section, meta FROM news_items
           WHERE status = 'new' AND fetched_at > now() - make_interval(days => $2)
           ORDER BY fetched_at, id LIMIT $1""", limit, TRIAGE_DAYS))


async def pending_count(conn) -> tuple[int, datetime | None]:
    row = await conn.fetchrow(
        "SELECT count(*) AS n, min(fetched_at) AS oldest FROM news_items "
        "WHERE status = 'new' AND fetched_at > now() - make_interval(days => $1)", TRIAGE_DAYS)
    return row["n"], row["oldest"]


async def set_verdict(conn, item_id: int, *, keep: bool, section: str | None, gist: str | None,
                      importance: int | None, reason: str | None) -> None:
    # model text: a "\u0000" or a cut "\ud83d" escape in its JSON decodes into text Postgres refuses
    await conn.execute(
        """UPDATE news_items SET status = $2, section = coalesce($3, section), gist = $4, importance = $5,
                                 drop_reason = $6
           WHERE id = $1 AND status = 'new'""",
        item_id, "kept" if keep else "rejected", section, _clean(gist), importance, reason)


async def triage_failed(conn, ids: list[int], max_attempts: int = 3) -> None:
    """The model skipped these items: retried later, rejected after a few attempts."""
    await conn.execute(
        """UPDATE news_items SET triage_attempts = triage_attempts + 1,
                                 status = CASE WHEN triage_attempts + 1 >= $2 THEN 'rejected' ELSE status END,
                                 drop_reason = CASE WHEN triage_attempts + 1 >= $2 THEN 'triage_failed' END
           WHERE id = ANY($1::bigint[]) AND status = 'new'""", ids, max_attempts)


# ---- the digest ----

async def candidates(conn, since: datetime) -> list[asyncpg.Record]:
    """Kept items fetched since `since`, whose story has not been in a digest yet."""
    return list(await conn.fetch(
        """SELECT i.id, i.source, i.url, i.title, i.summary, i.doi, i.meta, i.section, i.gist, i.importance,
                  i.published_at, i.fetched_at, coalesce(i.story_id, i.id) AS story_id
           FROM news_items i
           WHERE i.status = 'kept' AND i.fetched_at >= $1
             AND NOT EXISTS (SELECT 1 FROM news_items u WHERE u.status = 'used'
                             AND coalesce(u.story_id, u.id) = coalesce(i.story_id, i.id))
           ORDER BY i.id""", since))


async def digest_of(conn, day: date) -> str | None:
    return await conn.fetchval("SELECT text FROM news_digests WHERE day = $1", day)


async def save_digest(conn, day: date, text: str, item_ids: list[int]) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO news_digests (day, text, item_ids) VALUES ($1, $2, $3) ON CONFLICT (day) DO NOTHING",
            day, _clean(text), item_ids)
        await conn.execute("UPDATE news_items SET status = 'used', digest_day = $2 WHERE id = ANY($1::bigint[])",
                           item_ids, day)


async def log_llm(conn, scenario: str, completion, latency_ms: int, status: str = "ok") -> None:
    """Triage and the digest belong to no chat; their tokens still count toward the daily limit."""
    await conn.execute(
        """INSERT INTO bot_requests (scenario, answer, model, profile, input_tokens, output_tokens, latency_ms, status)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
        scenario, _clean(completion.text), completion.model, getattr(completion, "profile", None),
        completion.input_tokens, completion.output_tokens, latency_ms, status)


async def tokens_since(conn, start: datetime) -> int:
    return await conn.fetchval(
        "SELECT coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) "
        "FROM bot_requests WHERE created_at >= $1", start)


# ---- retention and health ----

async def cleanup(conn, now: datetime) -> None:
    await conn.execute("UPDATE news_items SET status = 'seen' WHERE status = 'new' AND fetched_at < $1",
                       now - timedelta(days=TRIAGE_DAYS))
    await conn.execute(
        """UPDATE news_items SET url = NULL, title = NULL, summary = NULL, gist = NULL, categories = NULL,
                                 meta = '{}', title_key = NULL
           WHERE fetched_at < $1 AND title IS NOT NULL""", now - timedelta(days=CONTENT_DAYS))
    await conn.execute("DELETE FROM news_items WHERE fetched_at < $1", now - timedelta(days=MARKER_DAYS))
    await conn.execute("DELETE FROM news_digests WHERE day < $1", (now - timedelta(days=CONTENT_DAYS)).date())


async def counts_since(conn, since: datetime) -> dict[str, dict[str, int]]:
    rows = await conn.fetch(
        "SELECT source, status, count(*) AS n FROM news_items WHERE fetched_at >= $1 GROUP BY source, status", since)
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["source"], {})[r["status"]] = r["n"]
    return out
