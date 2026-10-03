"""Collector service: owns the bot token, long-polls Telegram, writes every message to PostgreSQL.

Delivery guarantee: updates are handled one at a time (handle_as_tasks=False) and the
next getUpdates call, which confirms them to Telegram, only happens after the database
write committed. Database outages are retried indefinitely, so nothing is acknowledged
before it is stored; Telegram keeps unconfirmed updates for up to 24 hours.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

import asyncpg
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated, Message

from ..config import Settings
from . import store
from .parse import parse_chat, parse_message

log = logging.getLogger("nestor.collector")

ALLOWED_UPDATES = ["message", "edited_message", "my_chat_member"]
GROUP_TYPES = {"group", "supergroup"}

T = TypeVar("T")

# Errors after which the same write can succeed later: connection loss, server restart.
_TRANSIENT = (
    OSError,
    asyncio.TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
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
    """aiogram object -> Bot API JSON dict."""
    return obj.model_dump(mode="json", exclude_none=True, by_alias=True)


class Collector:
    def __init__(self, settings: Settings, pool: asyncpg.Pool):
        self.allowed = settings.allowed_chat_ids
        self.pool = pool
        self._allowed_cache: set[int] = set(self.allowed)

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
        if message.chat.type not in GROUP_TYPES:
            return  # private chats: commands arrive in stage 3
        msg = dump(message)
        if not await self.is_allowed(message.chat.id, msg):
            await leave(bot, message.chat.id)
            return
        await self.store(msg, is_edit=is_edit)

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


async def leave(bot: Bot, chat_id: int) -> None:
    """FR-7: never stay in a group that is not on the allowlist."""
    log.warning("leaving chat that is not on the allowlist", extra={"chat_id": chat_id})
    try:
        await bot.leave_chat(chat_id)
    except TelegramAPIError as e:
        log.warning("leave_chat failed", extra={"chat_id": chat_id, "error": type(e).__name__})


async def run(settings: Settings) -> None:
    if not settings.allowed_chat_ids:
        log.warning("ALLOWED_CHAT_IDS is empty: the bot will leave every group it is added to")
    pool = await store.create_pool(settings.database_url)
    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.include_router(Collector(settings, pool).router())
    try:
        await dp.start_polling(bot, handle_as_tasks=False, allowed_updates=ALLOWED_UPDATES)
    finally:
        await pool.close()
