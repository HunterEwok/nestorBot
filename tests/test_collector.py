"""Stage 2 acceptance through the real aiogram dispatcher: 0 losses, 0 duplicates, allowlist."""
import asyncio

import asyncpg
import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from nestor.collector.app import Collector, retry_forever
from nestor.config import Settings

from .factories import CHAT_ID, message, update

FOREIGN_CHAT = -1009999999999


class FakeBot(Bot):
    def __init__(self):
        super().__init__("42:TEST")
        self.left: list[int] = []

    async def leave_chat(self, chat_id, **kw):
        self.left.append(chat_id)
        return True


@pytest.fixture
def dispatcher(pool):
    settings = Settings(bot_token="42:TEST", database_url="", allowed_chat_ids=frozenset({CHAT_ID}))
    dp = Dispatcher()
    dp.include_router(Collector(settings, pool).router())
    return dp


async def feed(dp, bot, raw):
    await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))


async def test_thousand_messages_redelivered_no_loss_no_duplicates(dispatcher, conn):
    bot = FakeBot()
    updates = [update("message", message(i, f"сообщение {i}", uid=100 + i % 7)) for i in range(1, 1001)]
    for u in updates:
        await feed(dispatcher, bot, u)
    for u in updates[500:]:  # Telegram redelivers what was not confirmed before a crash
        await feed(dispatcher, bot, u)
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1000
    assert await conn.fetchval("SELECT count(DISTINCT message_id) FROM messages") == 1000
    assert await conn.fetchval("SELECT count(*) FROM users") == 7


async def test_edit_through_dispatcher(dispatcher, conn):
    bot = FakeBot()
    await feed(dispatcher, bot, update("message", message(1, "было")))
    await feed(dispatcher, bot, update("edited_message", message(1, "стало", edit_date=1_790_000_060)))
    assert await conn.fetchval("SELECT text FROM messages") == "стало"
    assert await conn.fetchval("SELECT old_text FROM message_edits") == "было"


async def test_message_with_link_preview_options_is_stored(dispatcher, conn):
    # aiogram fills unset LinkPreviewOptions fields with Default(...) placeholders that are not JSON.
    bot = FakeBot()
    msg = message(1, "смотри https://example.com", link_preview_options={"is_disabled": True})
    await feed(dispatcher, bot, update("message", msg))
    raw = await conn.fetchval("SELECT raw->'link_preview_options' FROM messages")
    assert raw is not None and "prefer_small_media" not in raw


async def test_foreign_group_is_left_and_not_stored(dispatcher, conn):
    bot = FakeBot()
    await feed(dispatcher, bot, update("message", message(1, "чужое", chat_id=FOREIGN_CHAT)))
    await feed(dispatcher, bot, update("my_chat_member", {
        "chat": {"id": FOREIGN_CHAT - 1, "type": "supergroup", "title": "x"},
        "from": {"id": 1, "is_bot": False, "first_name": "a"},
        "date": 1_790_000_000,
        "old_chat_member": {"status": "left", "user": {"id": 42, "is_bot": True, "first_name": "bot"}},
        "new_chat_member": {"status": "member", "user": {"id": 42, "is_bot": True, "first_name": "bot"}},
    }))
    assert bot.left == [FOREIGN_CHAT, FOREIGN_CHAT - 1]
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_private_chat_is_ignored(dispatcher, conn):
    bot = FakeBot()
    msg = message(1, "/start") | {"chat": {"id": 111, "type": "private", "first_name": "U"}}
    await feed(dispatcher, bot, update("message", msg))
    assert bot.left == [] and await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_retry_forever_waits_out_database_outage(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 4:
            raise asyncpg.CannotConnectNowError("starting up")
        return "ok"

    assert await retry_forever(flaky, what="test") == "ok" and calls == 4


async def _no_sleep(_):
    return None


async def test_retry_forever_waits_out_disk_full(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = 0

    async def disk_full():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise asyncpg.DiskFullError("could not extend file")
        return "ok"

    assert await retry_forever(disk_full, what="test") == "ok" and calls == 3


async def test_leave_retries_transient_telegram_errors(monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
    from aiogram.methods import LeaveChat

    from nestor.collector.app import leave

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class FlakyBot(FakeBot):
        def __init__(self, errors):
            super().__init__()
            self.errors = list(errors)

        async def leave_chat(self, chat_id, **kw):
            self.left.append(chat_id)
            if self.errors:
                raise self.errors.pop(0)
            return True

    method = LeaveChat(chat_id=FOREIGN_CHAT)
    bot = FlakyBot([TelegramNetworkError(method, "timeout"), TelegramNetworkError(method, "reset")])
    await leave(bot, FOREIGN_CHAT)
    assert len(bot.left) == 3

    bot = FlakyBot([TelegramForbiddenError(method, "bot is not a member")])
    await leave(bot, FOREIGN_CHAT)
    assert len(bot.left) == 1  # permanent: no retry
