"""Half a year of the charts' history from the Wayback Machine, once, on the first start.

The charts give only today; the Internet Archive kept some of their editions. For every chart whose
address it archived, its CDX index lists the snapshots of each day; each one is read with the chart's
own parser and saved as that day's edition, unless the bot already read that day itself. Days the
archive does not have stay gaps: the score fills a gap of up to two days and leaves longer ones out.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import date, timedelta

from ..news.http import Http, HttpError
from . import sources, store

log = logging.getLogger("nestor.trends")

DAYS = 183
CDX = "https://web.archive.org/cdx/search/cdx"
# The charts the archive can have: their address and how to read an archived copy.
ARCHIVED = {
    "shazam_ru": (sources.SHAZAM_URL, lambda r, day: sources.parse_shazam(r.text())),
    "shazam_msk": (sources.SHAZAM_MSK_URL, lambda r, day: sources.parse_shazam(r.text())),
    "apple_ru": (sources.APPLE_URL, lambda r, day: sources.parse_apple(r.json(), day)),
}


async def snapshots(http: Http, url: str, since: date, until: date) -> list[str]:
    """The archive's timestamps of `url`, one per day (the first of the day), answered 200."""
    r = await http.get(CDX, params={"url": url, "from": since.strftime("%Y%m%d"), "to": until.strftime("%Y%m%d"),
                                    "output": "json", "fl": "timestamp", "filter": "statuscode:200",
                                    "collapse": "timestamp:8"})
    rows = r.json() if r.body.strip() else []
    return [row[0] for row in rows[1:] if isinstance(row, list) and row and str(row[0]).isdigit()]


@contextlib.asynccontextmanager
async def _conn(db):
    """A connection of the pool only while it is needed: the copying takes a while."""
    if hasattr(db, "acquire"):
        async with db.acquire() as conn:
            yield conn
    else:
        yield db


async def backfill(db, http: Http, today: date, days: int = DAYS, progress=None) -> dict[str, int]:
    """Saves the archived editions of the last `days` days. db: a pool or a connection.
    Returns chart key -> days added. progress(key, added, total): after each snapshot, for the command line."""
    since = today - timedelta(days=days)
    added: dict[str, int] = {}
    for key, (url, parse) in ARCHIVED.items():
        async with _conn(db) as conn:
            have = await store.days(conn, key, since)
        try:
            stamps = await snapshots(http, url, since, today - timedelta(days=1))
        except (HttpError, ValueError) as e:
            log.warning("wayback index failed", extra={"source": key, "error": type(e).__name__})
            added[key] = 0
            continue
        todo = [ts for ts in stamps if date(int(ts[:4]), int(ts[4:6]), int(ts[6:8])) not in have]
        added[key] = 0
        for n, ts in enumerate(todo, 1):
            day = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
            try:
                # id_: the archived bytes as they were, without the archive's toolbar
                entries = parse(await http.get(f"https://web.archive.org/web/{ts}id_/{url}"), day)
            except Exception as e:  # noqa: BLE001 - a missing or broken copy: skip it, keep the rest
                log.info("wayback snapshot skipped", extra={"source": key, "status": type(e).__name__})
                entries = []
            if entries:
                async with _conn(db) as conn:
                    await store.save_chart(conn, day, key, entries, origin="wayback")
                added[key] += 1
            if progress:
                progress(key, added[key], len(todo))
    return added
