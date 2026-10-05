"""Collector service: owns the bot token, long-polls Telegram, writes every message to PostgreSQL.

Delivery guarantee: updates are handled one at a time (handle_as_tasks=False) and the
next getUpdates call, which confirms them to Telegram, only happens after the database
write committed. Database outages are retried indefinitely, so nothing is acknowledged
before it is stored; Telegram keeps unconfirmed updates for up to 24 hours.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable, TypeVar
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import Default
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import ChatMemberUpdated, Message

from ..config import Settings, check_alert_chat
from ..retention import Retention
from . import store
from .parse import parse_chat, parse_message
from .telemetry import TelegramProbe, Telemetry

log = logging.getLogger("nestor.collector")

ALLOWED_UPDATES = ["message", "edited_message", "my_chat_member"]
GROUP_TYPES = {"group", "supergroup"}

T = TypeVar("T")

# Errors after which the same write can succeed later: connection loss, server restart
# or shutdown, cancelled statements, a full disk or memory pressure on the database
# host, I/O errors, a read-only replica during failover. Retrying instead of skipping
# keeps the update unconfirmed until it is stored.
_TRANSIENT = (
    OSError,
    asyncio.TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.OperatorInterventionError,      # admin/crash shutdown, cannot connect now, query canceled
    asyncpg.InsufficientResourcesError,     # disk full, out of memory, too many connections
    asyncpg.PostgresSystemError,            # I/O error
    asyncpg.ReadOnlySQLTransactionError,
    asyncpg.SerializationError,
    asyncpg.DeadlockDetectedError,
)


async def retry_forever(op: Callable[[], Awaitable[T]], *, what: str, max_delay: float = 30.0) -> T:
    delay = 0.5
    while True:
        try:
            return await op()
        except _TRANSIENT as e:
            log.warning("database unavailable, retrying", extra={"op": what, "error": type(e).__name__, "delay": delay})
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


def dump(obj: Any) -> dict:
    """aiogram object -> Bot API JSON dict.

    Some aiogram types (LinkPreviewOptions, present whenever a text has a link and its
    preview options were set) hold Default(...) placeholders for fields Telegram did not
    send; they are not JSON, so they are dropped like None.
    """
    return _drop_none(obj.model_dump(mode="json", exclude_none=True, by_alias=True, fallback=_default_to_none))


def _default_to_none(value: Any) -> None:
    if isinstance(value, Default):
        return None
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


class Collector:
    def __init__(self, settings: Settings, pool: asyncpg.Pool):
        self.allowed = settings.allowed_chat_ids
        self.pool = pool
        self._allowed_cache: set[int] = set(self.allowed)
        self.answerer = None  # nestor.bot.answerer.Answerer, set when answering is enabled

    async def is_allowed(self, chat_id: int, msg: dict | None = None) -> bool:
        if chat_id in self._allowed_cache:
            return True
        if msg is not None and msg.get("migrate_from_chat_id") in self.allowed:
            self._allowed_cache.add(chat_id)
            return True

        async def lookup() -> bool:
            async with self.pool.acquire() as conn:
                return await store.allowed_by_migration(conn, chat_id, self.allowed)

        if await retry_forever(lookup, what="allowlist"):
            log.warning("chat allowed via group->supergroup migration; add it to ALLOWED_CHAT_IDS",
                        extra={"chat_id": chat_id})
            self._allowed_cache.add(chat_id)
            return True
        return False

    async def store(self, msg: dict, *, is_edit: bool) -> store.Result:
        chat = parse_chat(msg["chat"])
        row = parse_message(msg)

        async def write() -> store.Result:
            async with self.pool.acquire() as conn:
                return await store.save_message(conn, chat, row, is_edit=is_edit)

        result = await retry_forever(write, what="save_message")
        log.info("message", extra={
            "chat_id": row.chat_id, "message_id": row.message_id, "kind": row.kind,
            "edit": is_edit, "result": result.value,
        })
        return result

    async def handle_message(self, message: Message, bot: Bot, *, is_edit: bool) -> None:
        if message.chat.type == "private":
            # Never archived. Only /export is served there (the file with the person's own messages).
            if self.answerer is not None and not is_edit:
                try:
                    await self.answerer.on_private(dump(message))
                except Exception:  # noqa: BLE001
                    log.exception("private message handling failed")
            return
        if message.chat.type not in GROUP_TYPES:
            return
        msg = dump(message)
        if not await self.is_allowed(message.chat.id, msg):
            await leave(bot, message.chat.id)
            return
        result = await self.store(msg, is_edit=is_edit)
        # Only a first delivery may trigger an answer; edits and redeliveries never do.
        if self.answerer is not None and not is_edit and result in (store.Result.INSERTED, store.Result.PAUSED):
            try:
                await self.answerer.on_message(msg, paused=result is store.Result.PAUSED)
            except Exception:  # noqa: BLE001 - answering must never stop archiving
                log.exception("invocation handling failed", extra={"chat_id": message.chat.id})

    async def handle_membership(self, event: ChatMemberUpdated, bot: Bot) -> None:
        if event.chat.type not in GROUP_TYPES:
            return
        status = event.new_chat_member.status
        log.info("bot membership changed", extra={"chat_id": event.chat.id, "status": status})
        if status in ("member", "administrator", "restricted") and not await self.is_allowed(event.chat.id):
            await leave(bot, event.chat.id)

    def router(self) -> Router:
        router = Router(name="collector")

        @router.message()
        async def on_message(message: Message, bot: Bot) -> None:
            await self.handle_message(message, bot, is_edit=False)

        @router.edited_message()
        async def on_edit(message: Message, bot: Bot) -> None:
            await self.handle_message(message, bot, is_edit=True)

        @router.my_chat_member()
        async def on_membership(event: ChatMemberUpdated, bot: Bot) -> None:
            await self.handle_membership(event, bot)

        return router


async def leave(bot: Bot, chat_id: int, *, attempts: int = 6) -> None:
    """FR-7: never stay in a group that is not on the allowlist.

    Network and server errors are retried before the update is confirmed, so a
    single failed attempt in an otherwise quiet group does not keep the bot there.
    """
    log.warning("leaving chat that is not on the allowlist", extra={"chat_id": chat_id})
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            await bot.leave_chat(chat_id)
            return
        except TelegramRetryAfter as e:
            wait = float(e.retry_after)
        except (TelegramNetworkError, TelegramServerError):
            wait = delay
            delay = min(delay * 2, 30.0)
        except TelegramAPIError as e:  # forbidden, not found, bad request: already out or kicked
            log.warning("leave_chat failed", extra={"chat_id": chat_id, "error": type(e).__name__})
            return
        if attempt < attempts:
            await asyncio.sleep(wait)
    log.error("leave_chat kept failing; will retry on the next update from this chat", extra={"chat_id": chat_id})


def alert_chat(settings: Settings) -> int | None:
    """ALERT_CHAT_ID for /status and the watch on the watchdog; None while it cannot work (a warning says why)."""
    problem = check_alert_chat(settings)
    if problem:
        log.warning("/status and the watch on the watchdog are off", extra={"problem": problem})
        return None
    return settings.alert_chat_id


async def make_answerer(settings: Settings, bot: Bot, pool: asyncpg.Pool, collector: Collector, *,
                        alert_chat_id: int | None = None):
    from zoneinfo import ZoneInfo

    from ..bot.answerer import AnswerConfig, Answerer
    from ..bot.llm import HermesClient
    from ..bot.music import HttpChecker
    from ..bot.triggers import BotIdentity

    me = await bot.me()
    identity = BotIdentity(me.id, me.username, settings.bot_keywords)
    cfg = AnswerConfig(rate_limit=settings.rate_limit, rate_window_min=settings.rate_window_min,
                       daily_token_limit=settings.daily_token_limit, tz=ZoneInfo(settings.timezone),
                       music_daily_limit=settings.music_daily_limit,
                       music_player_app=settings.music_player_app, allowed_chat_ids=settings.allowed_chat_ids,
                       wisdom_chat_ids=settings.wisdom_chat_ids, digest_time=settings.digest_time,
                       digest_every_days=settings.digest_every_days,
                       wisdom_time=settings.wisdom_time, news_chat_ids=settings.news_chat_ids,
                       news_time=settings.news_time, news_max_topics=settings.news_max_topics,
                       reaction_chat_ids=settings.reaction_chat_ids, alert_chat_id=alert_chat_id)

    def client(name: str, url: str | None, key: str, timeout: float, missing: str) -> HermesClient | None:
        if not url:
            if missing:
                log.warning(missing)
            return None
        return HermesClient(url, key, settings.hermes_model, timeout=timeout, name=name)

    llm = client("group", settings.hermes_url, settings.hermes_api_key, cfg.timeout_s, "")
    web = client("web", settings.hermes_web_url, settings.hermes_web_api_key, cfg.timeout_s,
                 "HERMES_WEB_URL is not set: questions that need the internet get a refusal")
    smart = client("smart", settings.hermes_smart_url, settings.hermes_smart_api_key, cfg.smart_timeout_s,
                   "HERMES_SMART_URL is not set: songs, the wisdom of the day and the news digest use the web profile")
    deep = client("deep", settings.hermes_deep_url, settings.hermes_deep_api_key, cfg.smart_timeout_s, "")
    news_llm = client("news", settings.hermes_news_url, settings.hermes_news_api_key, cfg.timeout_s, "")
    answerer = Answerer(bot, pool, llm, identity, lambda msg: collector.store(msg, is_edit=False), cfg, web_llm=web,
                        smart_llm=smart, checker=HttpChecker(), deep_llm=deep, news_llm=news_llm,
                        routes=settings.model_routes)
    for task in ("theses", "news_triage", "news", "react"):
        log.info("model route", extra={"task": task, "profile": answerer.router.profile(task)})
    from ..news.service import NewsService
    from ..news.sources import all_sources

    answerer.news = NewsService(pool, all_sources(settings.news_telegram_channels),
                                triage_llm=answerer.router.llm("news_triage"), is_wanted=answerer.news_wanted,
                                tz=cfg.tz, daily_token_limit=cfg.daily_token_limit,
                                contact=settings.news_contact_email, timeout_s=cfg.smart_timeout_s)
    return answerer


async def run(settings: Settings) -> None:
    if not settings.allowed_chat_ids:
        log.warning("ALLOWED_CHAT_IDS is empty: the bot will leave every group it is added to")
    pool = await store.create_pool(settings.database_url)
    bot = Bot(settings.bot_token)
    # Every call to Telegram is counted for the heartbeat the watchdog reads (nestor.collector.telemetry).
    telemetry = Telemetry()
    bot.session.middleware(TelegramProbe(telemetry))
    dp = Dispatcher()
    collector = Collector(settings, pool)
    tz, alerts_to = ZoneInfo(settings.timezone), alert_chat(settings)
    # The nightly purge keeps the promise of /privacy whether or not the bot answers; the heartbeat too.
    background: list[asyncio.Task] = [
        asyncio.create_task(Retention(pool, tz=tz, at=settings.retention_time).run()),
        asyncio.create_task(telemetry.run(pool, bot=bot, alert_chat_id=alerts_to, tz=tz, quiet=settings.alert_quiet)),
    ]
    try:
        if settings.hermes_url:
            collector.answerer = await make_answerer(settings, bot, pool, collector, alert_chat_id=alerts_to)
        else:
            log.warning("HERMES_URL is not set: archiving only, the bot will not answer")
        dp.include_router(collector.router())
        if collector.answerer is not None:
            from ..bot.scheduler import Scheduler

            background.append(asyncio.create_task(Scheduler(collector.answerer).run()))
            if collector.answerer.news is not None:
                background.append(asyncio.create_task(collector.answerer.news.run()))
        await dp.start_polling(bot, handle_as_tasks=False, allowed_updates=ALLOWED_UPDATES)
    finally:
        for task in background:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await pool.close()
