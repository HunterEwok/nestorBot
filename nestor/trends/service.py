"""The charts loop inside the collector process, and the recommendation the answerer posts.

Every chart is read once a local day while any group wants the post (/music on); the weekly post
and /music now first read the charts not read today, then score, check and write. A recommendation
for the weekly post is made once a day and shared by every group that has it on.
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
from . import pick, score, sources, store

log = logging.getLogger("nestor.trends")

READ_TIMEOUT_S = 120
HISTORY_DAYS = 28
LISTENERS_AGE = timedelta(days=30)
LISTENER_LOOKUPS = 15               # Last.fm artist.getInfo calls per recommendation
TO_WRITE = 6                        # candidates the write-up model sees


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
                 daily_token_limit: int, lastfm_key: str = "", max_listeners: int = 0, contact: str = "",
                 http_factory: Callable[[str], Http] = Http, adapters=sources.ADAPTERS,
                 charts: tuple[sources.Chart, ...] = sources.CHARTS, tick_s: float = 900):
        self.pool, self.is_wanted, self.tz, self.daily_token_limit = pool, is_wanted, tz, daily_token_limit
        self.lastfm_key, self.max_listeners, self.contact = lastfm_key, max_listeners, contact
        self.http_factory, self.adapters, self.tick_s = http_factory, adapters, tick_s
        self.charts = tuple(c for c in charts if not c.needs_key or lastfm_key)
        self._lock = asyncio.Lock()         # one read of the charts at a time
        self._pick_lock = asyncio.Lock()    # one recommendation at a time
        self._cleaned: date | None = None

    async def run(self) -> None:
        from ..collector.app import _TRANSIENT

        while True:
            try:
                if await self.is_wanted():
                    now = datetime.now(timezone.utc)
                    await self.collect(now)
                    await self.cleanup(now)
            except _TRANSIENT as e:
                log.warning("music charts postponed", extra={"error": type(e).__name__})
            except Exception:  # noqa: BLE001 - never let a bug stop the loop
                log.exception("music charts failed")
            await asyncio.sleep(self.tick_s)

    def _today(self, now: datetime) -> date:
        return now.astimezone(self.tz).date()

    def due(self, state, now: datetime) -> bool:
        if state is None:
            return True
        if state["last_ok_day"] == self._today(now):
            return False
        return state["next_run_at"] is None or state["next_run_at"] <= now

    async def collect(self, now: datetime) -> dict[str, str]:
        """Read the charts not read today. Returns key -> status."""
        async with self._lock:
            async with self.pool.acquire() as conn:
                states = await store.states(conn)
            todo = [c for c in self.charts if self.due(states.get(c.key), now)]
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
            entries = await asyncio.wait_for(self.adapters[chart.kind](http, now, self.lastfm_key), READ_TIMEOUT_S)
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

    # ---- the recommendation ----

    async def _over_limit(self, conn, now: datetime) -> bool:
        start = now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return await news_store.tokens_since(conn, start) >= self.daily_token_limit

    async def recommend(self, day: date, *, check_llm: LLM | None, write_llm: LLM, checker: music.Checker,
                        player: str | None, timeout_s: float, force: bool = False) -> Post | None:
        """The post of `day`: NOTHING when nobody new is rising, None at the token limit.
        force (/music now): made afresh, not the day's shared post. NotEnoughMusic: no chart read yet."""
        async with self._pick_lock:
            if not force:
                async with self.pool.acquire() as conn:
                    made = await store.post_of(conn, day)
                if made is not None:
                    return _post(made)
            now = datetime.now(timezone.utc)
            await self.collect(now)
            async with self.pool.acquire() as conn:
                if await self._over_limit(conn, now):
                    return None
                rows = await store.history(conn, day - timedelta(days=HISTORY_DAYS))
            if not rows:
                raise NotEnoughMusic()
            candidates = await self._candidates(rows, day, now)
            candidates = await self._check(candidates, check_llm, now, timeout_s)
            post = await self._write(candidates[:TO_WRITE], day, now, write_llm, checker, player, timeout_s)
            if not force:
                async with self.pool.acquire() as conn:
                    await store.save_post(conn, day, post.html, post.plain, post.preview_url,
                                          post.keyboard.model_dump(mode="json", exclude_none=True)
                                          if post.keyboard else None)
                    return _post(await store.post_of(conn, day))
            return post

    async def _candidates(self, rows, day: date, now: datetime) -> list[score.Candidate]:
        keys = sorted({r["artist_key"] for r in rows})
        async with self.pool.acquire() as conn:
            artists = await store.artists(conn, keys)
        found = score.rising(rows, day, artists, now, max_listeners=self.max_listeners, limit=20)
        if not (self.lastfm_key and self.max_listeners):
            return found[:12]
        stale = [c for c in found if artists.get(c.key) is None or artists[c.key]["listeners_at"] is None
                 or artists[c.key]["listeners_at"] < now - LISTENERS_AGE][:LISTENER_LOOKUPS]
        if stale:
            async with self.http_factory(self.contact) as http:
                for c in stale:
                    try:
                        listeners = await sources.lastfm_listeners(http, self.lastfm_key,
                                                                     sources.main_artist(c.artist))
                    except Exception as e:  # noqa: BLE001 - without the number the artist just stays a candidate
                        log.warning("last.fm listeners failed", extra={"error": type(e).__name__})
                        continue
                    async with self.pool.acquire() as conn:
                        await store.set_listeners(conn, c.key, listeners, now)
            async with self.pool.acquire() as conn:
                artists = await store.artists(conn, keys)
            found = score.rising(rows, day, artists, now, max_listeners=self.max_listeners, limit=20)
        return found[:12]

    async def _check(self, candidates: list[score.Candidate], llm: LLM | None, now: datetime,
                     timeout_s: float) -> list[score.Candidate]:
        """The cheap model's verdict for the artists it has not judged lately; stars and foreign artists go."""
        if not candidates or llm is None:
            return candidates
        async with self.pool.acquire() as conn:
            artists = await store.artists(conn, [c.key for c in candidates])
        todo = [c for c in candidates if artists.get(c.key) is None or artists[c.key]["checked_at"] is None
                or artists[c.key]["checked_at"] < now - score.RECHECK]
        if not todo:
            return candidates
        started = time.monotonic()
        try:
            completion = await asyncio.wait_for(llm.complete(pick.check_prompt(todo)), timeout_s)
        except Exception as e:  # noqa: BLE001 - the write-up model is told to skip stars as well
            log.warning("music check failed", extra={"error": type(e).__name__})
            return candidates
        verdicts = pick.parse_check(completion.text, len(todo))
        dropped = set()
        async with self.pool.acquire() as conn:
            await news_store.log_llm(conn, "music_check", completion, int((time.monotonic() - started) * 1000),
                                     "ok" if verdicts else "error")
            for n, v in verdicts.items():
                await store.set_verdict(conn, todo[n - 1].key, known=v.known, russian=v.russian, now=now)
                if v.known or not v.russian:
                    dropped.add(todo[n - 1].key)
        return [c for c in candidates if c.key not in dropped]

    async def _write(self, candidates: list[score.Candidate], day: date, now: datetime, llm: LLM,
                     checker: music.Checker, player: str | None, timeout_s: float) -> Post:
        if not candidates:
            log.info("music: nobody new is rising")
            return NOTHING
        started = time.monotonic()
        completion = await asyncio.wait_for(llm.complete(pick.write_prompt(candidates, day)), timeout_s)
        plan = pick.parse_write(completion.text, candidates)
        async with self.pool.acquire() as conn:
            await news_store.log_llm(conn, "music_trends", completion, int((time.monotonic() - started) * 1000),
                                     "ok" if plan is not None else "error")
        if plan is None:
            raise LLMError("the music model returned no JSON")
        if plan.best is None:
            return NOTHING
        card = await music.build_card(plan.best, checker, player=player)
        text, plain = pick.render(plan, candidates, day, card)
        async with self.pool.acquire() as conn:
            await store.mark_recommended(conn, [candidates[n - 1].key for n, _ in plan.items], now)
        log.info("music recommendation", extra={"artists": len(plan.items)})
        return Post(text, plain, card.preview_url, card.keyboard)

    async def status(self, conn) -> tuple[list[tuple[sources.Chart, asyncpg.Record | None]], asyncpg.Record]:
        states = await store.states(conn)
        counts = await conn.fetchrow(
            "SELECT count(*) AS artists, count(*) FILTER (WHERE known) AS known, "
            "count(*) FILTER (WHERE recommended_at IS NOT NULL) AS recommended FROM music_artists")
        return [(c, states.get(c.key)) for c in self.charts], counts
