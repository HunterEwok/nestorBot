"""The news loop inside the collector process: fetch due sources, filter, store, triage. And
the digest, built once a day and posted by the answerer to every group that has it on.

It runs as its own task so that a slow source never delays reminders. Nothing is fetched
while no group wants the digest.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg

from ..bot.llm import LLM, LLMError
from . import digest, filters, rank, store, triage
from .adapters import ADAPTERS
from .http import Http, HttpError
from .model import Item, Source

log = logging.getLogger("nestor.news")

SOURCE_TIMEOUT_S = 300
PARALLEL = 4
MAX_AGE = timedelta(days=4)           # older entries are only marked as seen
FIRST_RUN_WINDOW = timedelta(hours=24)  # the first fetch keeps only the last day, the rest is history
TRIAGE_WAIT = timedelta(hours=3)      # a small backlog waits this long for company
CANDIDATE_WINDOW = timedelta(hours=36)
MIN_CANDIDATES = 3


class NewsService:
    def __init__(self, pool: asyncpg.Pool, sources: tuple[Source, ...], *, triage_llm: LLM | None,
                 is_wanted: Callable[[], Awaitable[bool]], tz: ZoneInfo, daily_token_limit: int, contact: str = "",
                 timeout_s: float = 120, http_factory: Callable[[str], Http] = Http, adapters=ADAPTERS,
                 tick_s: float = 300):
        self.pool, self.sources = pool, sources
        self.by_key = {s.key: s for s in sources}
        self.triage_llm, self.is_wanted = triage_llm, is_wanted
        self.tz, self.daily_token_limit, self.contact, self.timeout_s = tz, daily_token_limit, contact, timeout_s
        self.http_factory, self.adapters, self.tick_s = http_factory, adapters, tick_s
        self._lock = asyncio.Lock()          # one collect/triage pass at a time
        self._digest_lock = asyncio.Lock()   # one digest per day, shared by every group
        self._cleaned: date | None = None

    async def run(self) -> None:
        while True:
            if await self._stage("wanted", self.is_wanted):
                now = datetime.now(timezone.utc)
                # each stage on its own: a source that breaks collect must not stop triage and cleanup
                for name, stage in (("collect", self.collect), ("triage", self.triage), ("cleanup", self.cleanup)):
                    await self._stage(name, stage, now)
            await asyncio.sleep(self.tick_s)

    async def _stage(self, name: str, step: Callable[..., Awaitable], *args):
        """step(*args), or None if it failed: the loop goes on and the next tick tries again."""
        from ..collector.app import _TRANSIENT

        try:
            return await step(*args)
        except _TRANSIENT as e:  # database down: the next tick tries again
            log.warning("news pass postponed", extra={"stage": name, "error": type(e).__name__})
        except Exception:  # noqa: BLE001 - never let a bug stop the loop
            log.exception("news pass failed", extra={"stage": name})
        return None

    # ---- fetching ----

    def due(self, source: Source, state, now: datetime) -> bool:
        if state is None or state["last_run_at"] is None:
            return True
        if state["next_run_at"] is not None:
            return state["next_run_at"] <= now
        return state["last_run_at"] + timedelta(hours=source.every_h) <= now + timedelta(minutes=2)

    async def collect(self, now: datetime, only: tuple[str, ...] = ()) -> dict[str, str]:
        """Fetch the sources that are due (or the ones named). Returns key -> status."""
        async with self._lock:
            async with self.pool.acquire() as conn:
                states = await store.states(conn)
            if only:
                todo = [s for s in self.sources if s.key in only]
            else:
                todo = [s for s in self.sources if self.due(s, states.get(s.key), now)]
            if not todo:
                return {}
            limit = asyncio.Semaphore(PARALLEL)
            async with self.http_factory(self.contact) as http:
                async def one(source: Source) -> tuple[str, str]:
                    async with limit:
                        return source.key, await self._fetch(http, source, states.get(source.key), now)
                result = dict(await asyncio.gather(*(one(s) for s in todo)))
            async with self.pool.acquire() as conn:
                await store.link_stories(conn)
            return result

    async def _fetch(self, http, source: Source, state, now: datetime) -> str:
        cursor = dict(state["cursor"] or {}) if state is not None else {}
        seeded = bool(state["seeded"]) if state is not None else False
        failures = state["failures"] if state is not None else 0
        try:
            result = await asyncio.wait_for(self.adapters[source.kind](http, source, cursor, now), SOURCE_TIMEOUT_S)
        except HttpError as e:
            return await self._failed(source, now, f"http_{e.status}", failures, e.retry_after)
        except asyncio.TimeoutError:
            return await self._failed(source, now, "timeout", failures)
        except ValueError as e:
            return await self._failed(source, now, f"error: {str(e)[:80]}", failures)
        except Exception as e:  # noqa: BLE001 - network errors of aiohttp, a bug in one adapter
            log.warning("news source failed", extra={"source": source.key, "error": type(e).__name__})
            return await self._failed(source, now, f"error: {type(e).__name__}", failures)

        counts = {"new": 0, "seen": 0, "dropped": 0, "skipped": 0}
        try:
            newest = max((i.published_at for i in result.items
                          if i.published_at and i.published_at <= now + timedelta(hours=12)), default=None)
            async with self.pool.acquire() as conn, conn.transaction():
                for item in result.items:
                    status, reason = self._status(item, source, now, first_run=not seeded)
                    if status is None:
                        counts["skipped"] += 1
                        continue
                    if await store.save_item(conn, source, item, status, reason) is not None:
                        counts[status] += 1
                await store.save_run(conn, source.key, now, ok=True, status="skipped" if result.skipped else "ok",
                                     cursor=result.cursor, newest=newest, seeded=True)
        except Exception as e:  # noqa: BLE001 - a row the database refuses: back off instead of refetching every tick
            log.warning("news source not saved", extra={"source": source.key, "error": type(e).__name__})
            return await self._failed(source, now, f"error: {type(e).__name__}", failures)
        log.info("news source fetched", extra={"source": source.key, "items": len(result.items), **counts})
        return "skipped" if result.skipped else "ok"

    def _status(self, item: Item, source: Source, now: datetime, *, first_run: bool) -> tuple[str | None, str | None]:
        """(status, drop reason) for a fetched item; (None, None): not worth a row."""
        reason = filters.drop_reason(item, source)
        if reason is not None:
            return ("dropped", reason) if reason in filters.STORED_REASONS else (None, None)
        if first_run:
            fresh = item.published_at is not None and item.published_at >= now - FIRST_RUN_WINDOW
            return ("new" if fresh else "seen"), None
        if item.published_at is not None and item.published_at < now - MAX_AGE:
            return "seen", None
        return "new", None

    async def _failed(self, source: Source, now: datetime, status: str, failures: int,
                      retry_after: float | None = None) -> str:
        backoff = min(timedelta(minutes=15 * 2 ** min(failures, 5)), timedelta(hours=max(source.every_h, 1)))
        if retry_after:
            backoff = min(max(backoff, timedelta(seconds=retry_after)), timedelta(hours=24))
        async with self.pool.acquire() as conn:
            await store.save_run(conn, source.key, now, ok=False, status=status, next_run_at=now + backoff)
        log.warning("news source error", extra={"source": source.key, "status": status, "failures": failures + 1})
        return status

    # ---- triage ----

    def _day_start(self, now: datetime) -> datetime:
        return now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)

    async def _over_limit(self, conn, now: datetime) -> bool:
        return await store.tokens_since(conn, self._day_start(now)) >= self.daily_token_limit

    async def triage(self, now: datetime, *, force: bool = False, max_batches: int = 6) -> int:
        """Sort new items with the cheap model. Without force, a small backlog waits for more."""
        if self.triage_llm is None:
            return 0
        async with self._lock:
            async with self.pool.acquire() as conn:
                count, oldest = await store.pending_count(conn)
            if not count or (not force and count < triage.BATCH and oldest > now - TRIAGE_WAIT):
                return 0
            done = 0
            for _ in range(max_batches):
                async with self.pool.acquire() as conn:
                    if await self._over_limit(conn, now):
                        log.warning("news triage postponed: daily token limit reached")
                        break
                    rows = await store.pending(conn, triage.BATCH)
                if not rows:
                    break
                started = time.monotonic()
                try:
                    completion = await asyncio.wait_for(
                        self.triage_llm.complete(triage.build_prompt(rows, self.by_key)), self.timeout_s)
                except Exception as e:  # noqa: BLE001 - Hermes down or timed out: the items wait for the next pass
                    log.warning("news triage failed", extra={"error": type(e).__name__})
                    break
                verdicts = triage.parse(completion.text, rows)
                answered = {v.item_id for v in verdicts}
                async with self.pool.acquire() as conn, conn.transaction():
                    for v in verdicts:
                        await store.set_verdict(conn, v.item_id, keep=v.keep, section=v.section, gist=v.gist,
                                                importance=v.importance, reason=v.reason)
                    await store.triage_failed(conn, [r["id"] for r in rows if r["id"] not in answered])
                    await store.log_llm(conn, "news_triage", completion, int((time.monotonic() - started) * 1000),
                                        "ok" if verdicts else "error")
                done += len(verdicts)
                if not verdicts:  # prose or broken JSON: retry on a later pass, not three times right now
                    break
            return done

    # ---- the digest ----

    async def digest(self, day: date, llm: LLM, *, max_topics: int, timeout_s: float) -> str | None:
        """The day's digest as Telegram HTML, written at most once a day and shared by every group.
        None: not enough news today, the token limit is reached, or no model."""
        async with self._digest_lock:
            async with self.pool.acquire() as conn:
                cached = await store.digest_of(conn, day)
            if cached is not None:
                return cached
            now = datetime.now(timezone.utc)
            await self.triage(now, force=True, max_batches=10)
            async with self.pool.acquire() as conn:
                if await self._over_limit(conn, now):
                    return None
                rows = await store.candidates(conn, now - CANDIDATE_WINDOW)
            candidates = rank.pick(rows, self.by_key, now)
            if len(candidates) < MIN_CANDIDATES:
                log.info("news digest skipped: not enough news", extra={"candidates": len(candidates)})
                return None
            started = time.monotonic()
            completion = await asyncio.wait_for(
                llm.complete(digest.build_prompt(candidates, self.by_key, day, max_topics)), timeout_s)
            plan = digest.parse(completion.text, candidates, max_topics)
            async with self.pool.acquire() as conn:
                await store.log_llm(conn, "news", completion, int((time.monotonic() - started) * 1000),
                                    "ok" if plan else "error")
            if plan is None:
                raise LLMError("the digest model returned no usable topics")
            text, topics = digest.render(plan, candidates, self.by_key, day)
            async with self.pool.acquire() as conn:
                await store.save_digest(conn, day, text, digest.used_item_ids(topics, candidates))
                return await store.digest_of(conn, day)

    # ---- retention and health ----

    async def cleanup(self, now: datetime) -> None:
        today = now.astimezone(self.tz).date()
        if self._cleaned == today:
            return
        async with self.pool.acquire() as conn:
            await store.cleanup(conn, now)
            for line in await self.stale(conn, now):
                log.warning("news source stale", extra={"source": line})
        self._cleaned = today

    async def stale(self, conn, now: datetime) -> list[str]:
        states = await store.states(conn)
        out = []
        for s in self.sources:
            st = states.get(s.key)
            if st is None:
                continue
            if st["failures"] >= 3 or (st["newest_item_at"] is not None
                                       and st["newest_item_at"] < now - timedelta(days=s.max_age_days)):
                out.append(s.key)
        return out
