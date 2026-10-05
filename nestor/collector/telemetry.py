"""The collector's heartbeat for nestor.watchdog, and its watch on the watchdog.

TelegramProbe sits in the bot's session and counts what Telegram answers: every getUpdates (the
long poll) and every message the bot sends. Telemetry.run writes that once a minute into
ops_state('collector'), the row checks.check_heartbeat reads:

    updated_at        the heartbeat itself (the database's clock): collector.hung when it stops
    started_at        when this process started (ISO-8601 UTC, the Pi's clock)
    updates_ok_at     the last getUpdates that worked; telegram.polling when it is old
    updates_failures  failed getUpdates since then
    updates_error     the code of the last one: "401", "409", "network", "5xx" or the class name; null
    sent, send_failed, bad_request, flood    sendMessage/sendDocument since the start: delivered, failed,
                      refused as malformed (a formatting the bot retries as plain text), told to wait

Codes and counters only: no text Telegram sent back, no chat, no message.

The watchdog is the only one who sends alerts, so nobody would hear that it stopped: when its snapshot
in ops_state('watchdog') is missing or older than 20 minutes (and this process has run that long), the
collector says so in the alert group once, and once more, silently, when it is back. Whether it said
so is kept in ops_state('collector_watch'), so a restart does not say it again.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

log = logging.getLogger("nestor.collector")

POLL = "GetUpdates"
SENDS = ("SendMessage", "SendDocument")
EVERY_S = 60                            # the watchdog's collector.hung waits 3 minutes
WATCHDOG_STALE = timedelta(minutes=20)  # it runs every 5


def utcnow() -> datetime:
    return datetime.now(UTC)


def error_code(exc: BaseException) -> str:
    """What failed, as the watchdog reads it: 401 (the token), 409 (another poller), network, 5xx, or the class."""
    if isinstance(exc, TelegramUnauthorizedError):
        return "401"
    if isinstance(exc, TelegramConflictError):
        return "409"
    if isinstance(exc, TelegramNetworkError):
        return "network"
    if isinstance(exc, TelegramServerError):
        return "5xx"
    return type(exc).__name__


class Telemetry:
    def __init__(self, clock: Callable[[], datetime] = utcnow):
        self.clock = clock
        self.started_at = clock()
        self.updates_ok_at: datetime | None = None
        self.updates_failures = 0
        self.updates_error: str | None = None
        self.sent = self.send_failed = self.bad_request = self.flood = 0

    def ok(self, method: str) -> None:
        if method == POLL:
            self.updates_ok_at, self.updates_failures, self.updates_error = self.clock(), 0, None
        elif method in SENDS:
            self.sent += 1

    def failed(self, method: str, exc: BaseException) -> None:
        if method == POLL:
            self.updates_failures += 1
            self.updates_error = error_code(exc)
        elif method in SENDS:
            if isinstance(exc, TelegramBadRequest):
                self.bad_request += 1
            elif isinstance(exc, TelegramRetryAfter):
                self.flood += 1
            else:
                self.send_failed += 1

    def snapshot(self) -> dict:
        """The value of ops_state('collector'): what nestor.watchdog.checks.check_heartbeat reads."""
        return {"started_at": self.started_at.isoformat(),
                "updates_ok_at": self.updates_ok_at.isoformat() if self.updates_ok_at else None,
                "updates_failures": self.updates_failures, "updates_error": self.updates_error,
                "sent": self.sent, "send_failed": self.send_failed, "bad_request": self.bad_request,
                "flood": self.flood}

    async def run(self, pool: asyncpg.Pool, *, bot: Bot, alert_chat_id: int | None, tz: ZoneInfo,
                  quiet: tuple[time, time] | None = None, every_s: float = EVERY_S,
                  watchdog_stale: timedelta = WATCHDOG_STALE) -> None:
        while True:
            await self.tick(pool, bot=bot, alert_chat_id=alert_chat_id, tz=tz, quiet=quiet,
                            watchdog_stale=watchdog_stale)
            await asyncio.sleep(every_s)

    async def tick(self, pool: asyncpg.Pool, *, bot: Bot, alert_chat_id: int | None, tz: ZoneInfo,
                   quiet: tuple[time, time] | None = None, watchdog_stale: timedelta = WATCHDOG_STALE) -> None:
        from .app import _TRANSIENT

        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO ops_state (key, value, updated_at) VALUES ('collector', $1, now())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                    self.snapshot(), timeout=10)
            if alert_chat_id is not None:
                await self._watch(pool, bot, alert_chat_id, tz, quiet, watchdog_stale)
        except _TRANSIENT:
            pass            # the database is down: db.unreachable says it, the next tick writes again
        except Exception:  # the heartbeat must outlive a bug; nestor.logs keeps the exception's text out
            log.exception("telemetry failed")

    async def _watch(self, pool: asyncpg.Pool, bot: Bot, chat_id: int, tz: ZoneInfo,
                     quiet: tuple[time, time] | None, stale: timedelta) -> None:
        """WATCHDOG_DOWN once when the watchdog's snapshot is missing or stale, WATCHDOG_BACK silently after.
        Telegram is not waited for with a connection of the pool in hand."""
        from ..bot import prompts
        from ..watchdog import notify
        from ..watchdog.alerts import fmt_since, in_quiet

        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT updated_at, now() - updated_at > $1 AS stale FROM ops_state "
                                      "WHERE key = 'watchdog'", stale, timeout=10)
            said = (await conn.fetchval("SELECT value FROM ops_state WHERE key = 'collector_watch'", timeout=10)
                    or {}).get("down", False)
        now = self.clock()
        down = row is None or row["stale"]
        if down and not said and now - self.started_at >= stale:
            since = row["updated_at"] if row is not None else self.started_at
            text = prompts.WATCHDOG_DOWN.format(since=fmt_since(since, now, tz))
            if not await notify.send(bot, chat_id, text, silent=in_quiet(now.astimezone(tz).time(), quiet)):
                return      # said on a later tick
            log.warning("watchdog not running, said in the alert group")
        elif not down and said:
            if not await notify.send(bot, chat_id, prompts.WATCHDOG_BACK, silent=True):
                return
            log.info("watchdog back")
        else:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ops_state (key, value, updated_at) VALUES ('collector_watch', $1, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                {"down": down}, timeout=10)


class TelegramProbe(BaseRequestMiddleware):
    """In the bot's session (bot.session.middleware): counts each call's outcome for Telemetry and
    passes it on unchanged; an error is always raised again."""

    def __init__(self, telemetry: Telemetry):
        self.telemetry = telemetry

    async def __call__(self, make_request, bot, method):
        name = type(method).__name__
        try:
            response = await make_request(bot, method)
        except Exception as e:      # a cancelled long poll (shutdown) is not a failure
            self.telemetry.failed(name, e)
            raise
        self.telemetry.ok(name)
        return response
