"""The charts loop inside the collector process, and the viral song of the day the answerer posts.

Every chart is read once a local day while any group wants the post (/music on); on the first start
half a year of their history is copied from the Wayback Machine (backfill.py). The daily post and
/music now first read the charts not read today, then pick the song (viral.py) and have the model
write about it. The daily post is made once a day and shared by every group that has it on.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import InlineKeyboardMarkup

from ..bot import music
from ..bot.llm import LLM, LLMError
from ..news import store as news_store
from ..news.http import Http, HttpError
from . import pick, sources, store, viral

log = logging.getLogger("nestor.trends")

READ_TIMEOUT_S = 120
REFUSED = frozenset({"http_401", "http_403", "http_451"})
REFUSED_BACKOFF = timedelta(hours=24)
RETRY_EVERY_S = 3600                # /music now asks failed charts again at most this often, from any group


class NotEnoughMusic(Exception):
    """No chart read yet: worth another try later."""


@dataclass(frozen=True)
class Post:
    html: str                       # "": nobody worth it
    plain: str = ""
    preview_url: str | None = None
    keyboard: InlineKeyboardMarkup | None = None


NOTHING = Post("")


def _post(row) -> Post:
    keyboard = InlineKeyboardMarkup.model_validate(row["keyboard"]) if row["keyboard"] else None
    return Post(row["html"], row["plain"], row["preview_url"], keyboard)


class TrendsService:
    def __init__(self, pool: asyncpg.Pool, *, is_wanted: Callable[[], Awaitable[bool]], tz: ZoneInfo,
                 daily_token_limit: int, lastfm_key: str = "", contact: str = "",
                 yandex_proxy: str = "",
                 http_factory: Callable[[str], Http] = Http, adapters=sources.ADAPTERS,
                 charts: tuple[sources.Chart, ...] = sources.CHARTS, tick_s: float = 900):
        self.pool, self.is_wanted, self.tz, self.daily_token_limit = pool, is_wanted, tz, daily_token_limit
        self.lastfm_key, self.contact = lastfm_key, contact
        self.opts = sources.Options(lastfm_key, yandex_proxy)
        self.http_factory, self.adapters, self.tick_s = http_factory, adapters, tick_s
        self.charts = tuple(c for c in charts if not c.needs_key or lastfm_key)
        self._lock = asyncio.Lock()         # one read of the charts at a time
        self._pick_lock = asyncio.Lock()    # one recommendation at a time
        self._cleaned: date | None = None
        self._retried = False               # the first read after a start ignores the pauses (see collect)
        self._forced_at: float | None = None    # monotonic time of the last /music now that ignored them

    async def run(self) -> None:
        from ..collector.app import _TRANSIENT

        while True:
            try:
                if await self.is_wanted():
                    now = datetime.now(timezone.utc)
                    await self.collect(now)
                    await self.cleanup(now)
                    await self.backfill(now)
            except _TRANSIENT as e:
                log.warning("music charts postponed", extra={"error": type(e).__name__})
            except Exception:  # noqa: BLE001 - never let a bug stop the loop
                log.exception("music charts failed")
            await asyncio.sleep(self.tick_s)

    async def backfill(self, now: datetime) -> None:
        """Once: the charts' history from the Wayback Machine. Marked done in music_sources as 'wayback' only when
        every chart's index was read; otherwise a later tick tries again, skipping the days already copied."""
        async with self.pool.acquire() as conn:
            if (await store.states(conn)).get("wayback") is not None:
                return
        from .backfill import backfill

        async with self.http_factory(self.contact) as http:
            added = await backfill(self.pool, http, self._today(now))
        log.info("music history from the wayback machine", extra={k: v for k, v in added.items()})
        if None in added.values():
            return
        async with self.pool.acquire() as conn:
            await store.save_run(conn, "wayback", now, status=f"ok: {sum(added.values())}", day=self._today(now),
                                 size=sum(added.values()))

    def _today(self, now: datetime) -> date:
        return now.astimezone(self.tz).date()

    def due(self, state, now: datetime, retry: bool = False) -> bool:
        """retry: a chart that failed is read again now, whatever pause its failure set."""
        if state is None:
            return True
        if state["last_ok_day"] == self._today(now):
            return False
        return retry or state["next_run_at"] is None or state["next_run_at"] <= now

    async def collect(self, now: datetime, *, retry: bool = False) -> dict[str, str]:
        """Read the charts not read today. Returns key -> status.

        retry (/music now), and the first read after a start: the failed charts too, before their pause is over.
        A restart is how a setting changes (MUSIC_YANDEX_PROXY after a 451), so it should not wait a day."""
        async with self._lock:
            if retry and self._forced_at is not None and time.monotonic() - self._forced_at < RETRY_EVERY_S:
                retry = False       # however often /music now is asked, a refusing source sees it once an hour
            async with self.pool.acquire() as conn:
                states = await store.states(conn)
            forced = retry
            retry, self._retried = retry or not self._retried, True   # only once the states are known
            todo = [c for c in self.charts if self.due(states.get(c.key), now, retry)]
            if forced and any(not self.due(states.get(c.key), now) for c in todo):
                self._forced_at = time.monotonic()      # only when it read a chart that was waiting out a pause
            if not todo:
                return {}
            async with self.http_factory(self.contact) as http:
                result = {}
                for chart in todo:      # four requests a day: no need for parallel reads
                    state = states.get(chart.key)
                    result[chart.key] = await self._read(http, chart, state["failures"] if state else 0, now)
            return result

    async def _read(self, http, chart: sources.Chart, failures: int, now: datetime) -> str:
        try:
            entries = await asyncio.wait_for(self.adapters[chart.kind](http, now, self.opts), READ_TIMEOUT_S)
            status = "ok" if entries else "empty"
        except HttpError as e:
            entries, status = [], f"http_{e.status}"
        except asyncio.TimeoutError:
            entries, status = [], "timeout"
        except ValueError as e:
            entries, status = [], f"error: {str(e)[:80]}"
        except Exception as e:  # noqa: BLE001 - network errors of aiohttp, a bug in one adapter
            entries, status = [], f"error: {type(e).__name__}"
        async with self.pool.acquire() as conn:
            if entries:
                await store.save_chart(conn, self._today(now), chart.key, entries)
                await store.save_run(conn, chart.key, now, status=status, day=self._today(now), size=len(entries))
            else:
                backoff = min(timedelta(minutes=15 * 2 ** min(failures, 5)), timedelta(hours=6))
                if status in REFUSED:      # the service will not serve us (Yandex outside Russia: 451)
                    backoff = REFUSED_BACKOFF
                await store.save_run(conn, chart.key, now, status=status, next_run_at=now + backoff)
        log.log(logging.INFO if entries else logging.WARNING, "music chart read",
                extra={"source": chart.key, "status": status, "entries": len(entries)})
        return status

    async def cleanup(self, now: datetime) -> None:
        today = self._today(now)
        if self._cleaned != today:
            async with self.pool.acquire() as conn:
                await store.cleanup(conn, today)
            self._cleaned = today

    # ---- the song of the day ----

    async def _over_limit(self, conn, now: datetime) -> bool:
        start = now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return await news_store.tokens_since(conn, start) >= self.daily_token_limit

    async def candidates(self, conn, day: date, *, picks: list[viral.Pick] | None = None,
                         limit: int = 5) -> list[viral.Candidate]:
        """The viral songs of `day` from the snapshots up to it. picks: the cooldowns; None reads them from the base."""
        rows = await store.history(conn, day - timedelta(days=viral.HISTORY_DAYS))
        if picks is None:
            picks = await store.picks(conn, day - timedelta(days=viral.SONG_COOLDOWN))
        return viral.viral(rows, day, picks=picks, banned=await store.banned(conn), limit=limit)

    async def recommend(self, day: date, *, write_llm: LLM, checker: music.Checker, player: str | None,
                        timeout_s: float, force: bool = False) -> Post | None:
        """The post of `day`: NOTHING when no song went viral, None at the token limit.
        force (/music now): made afresh, not the day's shared post. NotEnoughMusic: no chart read yet."""
        async with self._pick_lock:
            if not force:
                async with self.pool.acquire() as conn:
                    made = await store.post_of(conn, day)
                if made is not None:
                    return _post(made)
            now = datetime.now(timezone.utc)
            await self.collect(now, retry=force)
            async with self.pool.acquire() as conn:
                if await self._over_limit(conn, now):
                    return None
                if not await conn.fetchval("SELECT 1 FROM music_snapshots WHERE day >= $1 LIMIT 1",
                                           day - timedelta(days=viral.HISTORY_DAYS)):
                    raise NotEnoughMusic()
                found = await self.candidates(conn, day, limit=1)
            post = await self._write(found[0], day, write_llm, checker, player, timeout_s) if found else NOTHING
            if not found:
                log.info("music: no viral song today")
            if not force:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        if found:
                            await store.save_pick(conn, day, found[0])
                        await store.save_post(conn, day, post.html, post.plain, post.preview_url,
                                              post.keyboard.model_dump(mode="json", exclude_none=True)
                                              if post.keyboard else None)
                    return _post(await store.post_of(conn, day))
            return post

    async def _write(self, c: viral.Candidate, day: date, llm: LLM, checker: music.Checker, player: str | None,
                     timeout_s: float) -> Post:
        started = time.monotonic()
        completion = await asyncio.wait_for(llm.complete(pick.write_prompt(c, day)), timeout_s)
        plan = pick.parse_write(completion.text, c)
        async with self.pool.acquire() as conn:
            await news_store.log_llm(conn, "music_trends", completion, int((time.monotonic() - started) * 1000),
                                     "ok" if plan is not None else "error")
        if plan is None:
            raise LLMError("the music model returned no JSON")
        card = await music.build_card(plan.best, checker, player=player)
        text, plain = pick.render(plan, c, day, card)
        log.info("music: the song of the day", extra={"score": c.score})
        return Post(text, plain, card.preview_url, card.keyboard)

    async def status(self, conn) -> tuple[list[tuple[sources.Chart, asyncpg.Record | None]], asyncpg.Record]:
        states = await store.states(conn)
        counts = await conn.fetchrow(
            "SELECT (SELECT count(DISTINCT day) FROM music_snapshots) AS days, "
            "(SELECT count(*) FROM music_picks) AS picks")
        return [(c, states.get(c.key)) for c in self.charts], counts
