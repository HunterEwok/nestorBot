"""Stage 4 collector telemetry: what the bot's session counts (a Bot API on 127.0.0.1 answers as Telegram
does), the heartbeat in ops_state('collector') as the watchdog reads it, and the collector's watch on the
watchdog itself."""
import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates
from aiohttp import web

from nestor import retention
from nestor.bot import prompts
from nestor.collector import app, telemetry
from nestor.collector.telemetry import TelegramProbe, Telemetry
from nestor.config import Settings
from nestor.logs import JsonFormatter
from nestor.watchdog import alerts, run

from .factories import CHAT_ID
from .test_watchdog_checks import ALERT_CHAT, BY_NAME, AlertBot, _beat, make_ctx

MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TOKEN = "42:TEST"
# The heartbeat's keys, as the watchdog's tests write it (test_watchdog_checks._beat).
CONTRACT = {"started_at", "updates_ok_at", "updates_failures", "updates_error", "sent", "send_failed", "bad_request",
            "flood"}
SENT = {"message_id": 1, "date": 1_790_000_000, "chat": {"id": CHAT_ID, "type": "supergroup"}, "text": "ok"}
RESULTS = {"getUpdates": [], "sendMessage": SENT, "sendDocument": SENT,
           "getMe": {"id": 42, "is_bot": True, "first_name": "Нестор", "username": "ewok_nestor_bot"}}


def refusal(status: int, description: str, **parameters) -> tuple[int, dict]:
    return status, {"ok": False, "error_code": status, "description": description,
                    **({"parameters": parameters} if parameters else {})}


CONFLICT = refusal(409, "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running")
UNAUTHORIZED = refusal(401, "Unauthorized")
BAD_GATEWAY = refusal(502, "Bad Gateway")
BAD_REQUEST = refusal(400, "Bad Request: can't parse entities: unsupported start tag")
FLOOD = refusal(429, "Too Many Requests: retry after 7", retry_after=7)


class Clock:
    def __init__(self, at: datetime = T0):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
async def telegram():
    """The Bot API on 127.0.0.1: each method answers what the test queued for it, otherwise success."""
    queued: dict[str, list[tuple[int, dict]]] = defaultdict(list)

    async def handle(request):
        method = request.match_info["method"]
        status, body = queued[method].pop(0) if queued[method] else (200, {"ok": True, "result": RESULTS[method]})
        return web.json_response(body, status=status)

    server = web.Application()
    server.router.add_post("/bot{token}/{method}", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    bots = []

    def make(tel: Telemetry, base: str = url) -> Bot:
        bot = Bot(TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(base)))
        bot.session.middleware(TelegramProbe(tel))
        bots.append(bot)
        return bot

    make.queue = lambda method, *replies: queued[method].extend(replies)
    yield make
    for bot in bots:
        await bot.session.close()
    await runner.cleanup()


# ---- the probe in the bot's session ----------------------------------------------------------------------------

async def test_probe_records_get_updates_ok_and_409(telegram):
    clock = Clock()
    tel = Telemetry(clock=clock)
    bot = telegram(tel)
    assert set(tel.snapshot()) == CONTRACT
    assert tel.snapshot() == {"started_at": T0.isoformat(), "updates_ok_at": None, "updates_failures": 0,
                              "updates_error": None, "sent": 0, "send_failed": 0, "bad_request": 0, "flood": 0}
    clock.at = T0 + timedelta(seconds=30)
    assert await bot.get_updates() == []
    assert (tel.updates_ok_at, tel.updates_failures, tel.updates_error) == (clock.at, 0, None)
    # Another process polls with the same token: Telegram says 409, and says it again.
    telegram.queue("getUpdates", CONFLICT, CONFLICT)
    clock.at += timedelta(minutes=1)
    for _ in range(2):
        with pytest.raises(TelegramConflictError):
            await bot.get_updates()
    assert (tel.updates_ok_at, tel.updates_failures, tel.updates_error) == (T0 + timedelta(seconds=30), 2, "409")
    # Each failure leaves its code; the token revoked, Telegram's servers, the Pi's network.
    telegram.queue("getUpdates", UNAUTHORIZED, BAD_GATEWAY)
    for raised, code in ((TelegramUnauthorizedError, "401"), (TelegramServerError, "5xx")):
        with pytest.raises(raised):
            await bot.get_updates()
        assert tel.updates_error == code
    with pytest.raises(TelegramNetworkError):
        await telegram(tel, "http://127.0.0.1:1").get_updates()
    assert (tel.updates_failures, tel.updates_error) == (5, "network")
    # Sends and other calls leave the poll alone; a good poll clears it.
    await bot.get_me()
    assert (tel.updates_failures, tel.updates_error) == (5, "network")
    clock.at += timedelta(minutes=1)
    await bot.get_updates()
    assert (tel.updates_ok_at, tel.updates_failures, tel.updates_error) == (clock.at, 0, None)
    snap = tel.snapshot()
    assert (snap["updates_ok_at"], snap["sent"]) == (clock.at.isoformat(), 0) and json.loads(json.dumps(snap)) == snap


async def test_bad_request_and_flood_counted_apart(telegram):
    tel = Telemetry()
    bot = telegram(tel)
    await bot.send_message(CHAT_ID, "<b>ok</b>", parse_mode="HTML")
    telegram.queue("sendMessage", BAD_REQUEST, FLOOD, BAD_GATEWAY)
    for raised in (TelegramBadRequest, TelegramRetryAfter, TelegramServerError):
        with pytest.raises(raised):
            await bot.send_message(CHAT_ID, "<b>no")
    await bot.send_document(CHAT_ID, document="file-id")
    with pytest.raises(TelegramNetworkError):
        await telegram(tel, "http://127.0.0.1:1").send_message(CHAT_ID, "x")
    # A refused formatting is retried as plain text and Telegram's flood control passes: neither is a lost message.
    assert (tel.sent, tel.bad_request, tel.flood, tel.send_failed) == (2, 1, 1, 2)
    assert (tel.updates_ok_at, tel.updates_failures) == (None, 0)


async def test_probe_reraises():
    tel = Telemetry()
    probe = TelegramProbe(tel)
    boom = RuntimeError("unexpected")

    async def fails(bot, method):
        raise boom

    with pytest.raises(RuntimeError) as raised:
        await probe(fails, None, GetUpdates())
    assert raised.value is boom and tel.updates_error == "RuntimeError"

    async def answers(bot, method):
        return "the response"

    assert await probe(answers, None, GetUpdates()) == "the response" and tel.updates_failures == 0

    async def cancelled(bot, method):
        raise asyncio.CancelledError

    # The long poll cancelled at shutdown: not a failure.
    with pytest.raises(asyncio.CancelledError):
        await probe(cancelled, None, GetUpdates())
    assert (tel.updates_failures, tel.updates_error) == (0, None)


# ---- the heartbeat ---------------------------------------------------------------------------------------------

class FlakyPool:
    """The pool, after the given errors of acquire()."""

    def __init__(self, pool, *errors: Exception):
        self.pool, self.errors = pool, list(errors)

    def acquire(self):
        if self.errors:
            raise self.errors.pop(0)
        return self.pool.acquire()


async def test_run_writes_ops_state_and_survives_transient(pool, conn, caplog):
    tel = Telemetry()
    tel.ok("GetUpdates")
    tel.failed("SendMessage", TelegramBadRequest(method=None, message="Bad Request: КАНАРЕЙКА"))
    flaky = FlakyPool(pool, ConnectionRefusedError(), RuntimeError("КАНАРЕЙКА in a bug"))
    with caplog.at_level(logging.WARNING):
        # The database refuses: nothing to say, the next tick writes. A bug: logged, by its class.
        await tel.tick(flaky, bot=None, alert_chat_id=None, tz=MSK)
        assert caplog.records == []
        await tel.tick(flaky, bot=None, alert_chat_id=None, tz=MSK)
        assert [r.getMessage() for r in caplog.records] == ["telemetry failed"]
        assert "КАНАРЕЙКА" not in JsonFormatter().format(caplog.records[0])
        assert await conn.fetchval("SELECT count(*) FROM ops_state") == 0
        await tel.tick(flaky, bot=None, alert_chat_id=None, tz=MSK)
    row = await conn.fetchrow("SELECT value, now() - updated_at AS age FROM ops_state WHERE key = 'collector'")
    assert row["value"] == tel.snapshot() and row["age"] < timedelta(seconds=5)
    assert (row["value"]["bad_request"], row["value"]["updates_error"]) == (1, None)
    # The loop: a minute apart in production; here the counters it writes change between ticks.
    task = asyncio.create_task(tel.run(pool, bot=None, alert_chat_id=None, tz=MSK, every_s=0.01))
    try:
        tel.sent = 7
        for _ in range(500):
            await asyncio.sleep(0.01)
            if (await conn.fetchval("SELECT value FROM ops_state WHERE key = 'collector'"))["sent"] == 7:
                break
        assert (await conn.fetchval("SELECT value FROM ops_state WHERE key = 'collector'"))["sent"] == 7
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert await conn.fetchval("SELECT count(*) FROM ops_state") == 1   # no watch without an alert group


async def test_heartbeat_is_what_the_watchdog_reads(pool, conn):
    clock = Clock(await conn.fetchval("SELECT clock_timestamp()"))
    tel = Telemetry(clock=clock)
    tel.ok("GetUpdates")
    await tel.tick(pool, bot=None, alert_chat_id=None, tz=MSK)
    ctx = await make_ctx(conn, now=clock.at)
    assert (await run.run_check(ctx, BY_NAME["heartbeat"])).status == "ok"
    # Another poller: said at once.
    tel.failed("GetUpdates", TelegramConflictError(method=None, message="Conflict"))
    await tel.tick(pool, bot=None, alert_chat_id=None, tz=MSK)
    res = await run.run_check(await make_ctx(conn, now=clock.at), BY_NAME["heartbeat"])
    assert [(f.key, f.line) for f in res.findings] == [("telegram.polling", prompts.ALERT_POLLING_CONFLICT)]
    # Telegram's servers failing for 10 minutes: polling, from the last good poll.
    tel.ok("GetUpdates")
    good = clock.at
    clock.at += timedelta(minutes=10)
    for _ in range(3):
        tel.failed("GetUpdates", TelegramServerError(method=None, message="Bad Gateway"))
    await tel.tick(pool, bot=None, alert_chat_id=None, tz=MSK)
    now = clock.at
    res = await run.run_check(await make_ctx(conn, now=now), BY_NAME["heartbeat"])
    assert [(f.key, f.line) for f in res.findings] == [
        ("telegram.polling", prompts.ALERT_POLLING.format(since=alerts.fmt_since(good, now, MSK), error="<code>5xx</code>"))]
    # The watchdog's own fixture writes the same keys.
    await _beat(conn)
    assert set(await conn.fetchval("SELECT value FROM ops_state WHERE key = 'collector'")) == set(tel.snapshot())


# ---- the watch on the watchdog ---------------------------------------------------------------------------------

async def _watchdog_ran(conn, ago: str = "0 seconds") -> None:
    await conn.execute("""INSERT INTO ops_state (key, value, updated_at) VALUES ('watchdog', '{}', now() - $1::text::interval)
                          ON CONFLICT (key) DO UPDATE SET updated_at = EXCLUDED.updated_at""", ago)


async def test_collector_reports_missing_watchdog_once_and_recovery_silently(pool, conn):
    clock = Clock(datetime(2026, 10, 5, 12, 0, tzinfo=MSK))
    bot = AlertBot()

    async def tick(tel=None, **kw):
        await (tel or watch).tick(pool, bot=bot, alert_chat_id=ALERT_CHAT, tz=MSK, **kw)

    watch = Telemetry(clock=clock)
    # Just started: the watchdog may not have had its turn yet.
    clock.at += timedelta(minutes=19)
    await tick()
    assert bot.sent == []
    clock.at += timedelta(minutes=1)
    await tick()
    assert [(m["chat_id"], m["text"], m["disable_notification"]) for m in bot.sent] == [
        (ALERT_CHAT, prompts.WATCHDOG_DOWN.format(since="12:00"), False)]
    assert bot.sent[0]["parse_mode"] == "HTML" and bot.sent[0]["link_preview_options"].is_disabled
    # Once: not on the next tick, not after the collector restarts.
    clock.at += timedelta(minutes=30)
    await tick()
    restarted = Telemetry(clock=Clock(clock.at - timedelta(hours=1)))
    restarted.clock = clock
    await tick(restarted)
    assert len(bot.sent) == 1
    # It runs again: said once, silently.
    await _watchdog_ran(conn)
    await tick()
    await tick()
    assert [(m["text"], m["disable_notification"]) for m in bot.sent[1:]] == [(prompts.WATCHDOG_BACK, True)]
    # It stops: counted from its last snapshot; at night without sound.
    await _watchdog_ran(conn, "25 minutes")
    last = await conn.fetchval("SELECT updated_at FROM ops_state WHERE key = 'watchdog'")
    clock.at = datetime(2026, 10, 6, 2, 0, tzinfo=MSK)
    await tick(quiet=(time(23), time(8)))
    assert bot.sent[-1] == {**bot.sent[-1], "text": prompts.WATCHDOG_DOWN.format(since=alerts.fmt_since(last, clock.at, MSK)),
                            "disable_notification": True}
    # Telegram down when it is time to say it: said on a later tick.
    await _watchdog_ran(conn)
    bot.errors = [TelegramNetworkError(method=None, message="down")]
    await tick()
    assert len(bot.sent) == 3
    await tick()
    assert bot.sent[-1]["text"] == prompts.WATCHDOG_BACK and len(bot.sent) == 4
    assert await conn.fetchval("SELECT value FROM ops_state WHERE key = 'collector_watch'") == {"down": False}


async def test_a_database_outage_is_not_the_watchdog_stopping(pool, conn):
    # Neither can write while the database is down, and once it is back the collector usually looks first.
    clock = Clock(datetime(2026, 10, 5, 12, 0, tzinfo=MSK))
    watch = Telemetry(clock=clock)
    clock.at += timedelta(hours=2)                      # it has run for two hours
    bot = AlertBot()

    async def tick(target=pool):
        await watch.tick(target, bot=bot, alert_chat_id=ALERT_CHAT, tz=MSK)

    await _watchdog_ran(conn)
    await tick()
    down = FlakyPool(pool, *[ConnectionRefusedError() for _ in range(30)])
    for _ in range(30):                                 # the router reboots for half an hour
        clock.at += timedelta(minutes=1)
        await tick(down)
    await _watchdog_ran(conn, "31 minutes")             # its last snapshot is from before
    for minutes in (1, 19):                             # back: the watchdog gets 20 minutes of it to write
        clock.at += timedelta(minutes=minutes)
        await tick()
    assert bot.sent == []
    clock.at += timedelta(minutes=1)                    # it did not: now it is news
    await tick()
    last = await conn.fetchval("SELECT updated_at FROM ops_state WHERE key = 'watchdog'")
    assert [(m["text"], m["disable_notification"]) for m in bot.sent] == [
        (prompts.WATCHDOG_DOWN.format(since=alerts.fmt_since(last, clock.at, MSK)), False)]


# ---- the collector -------------------------------------------------------------------------------------------

async def test_collector_starts_the_heartbeat_with_its_probe_and_the_alert_group(monkeypatch, caplog):
    started = []

    async def idle(self):
        await asyncio.Event().wait()

    async def heartbeat(self, pool, **kw):
        started.append(kw)
        await asyncio.Event().wait()

    class NoPool:
        async def close(self):
            pass

    async def create_pool(dsn):
        return NoPool()

    async def start_polling(self, bot, **kw):
        await asyncio.sleep(0)                      # the tasks get to run
        started.append([type(m).__name__ for m in bot.session.middleware])

    monkeypatch.setattr(retention.Retention, "run", idle)
    monkeypatch.setattr(telemetry.Telemetry, "run", heartbeat)
    monkeypatch.setattr(app.store, "create_pool", create_pool)
    monkeypatch.setattr(app.Dispatcher, "start_polling", start_polling)
    base = Settings(bot_token=TOKEN, database_url="", allowed_chat_ids=frozenset({CHAT_ID, ALERT_CHAT}),
                    alert_chat_id=ALERT_CHAT, alert_quiet=(time(22), time(7)), timezone="Europe/Tallinn")
    await app.run(base)
    assert started == [{"bot": started[0]["bot"], "alert_chat_id": ALERT_CHAT, "tz": ZoneInfo("Europe/Tallinn"),
                        "quiet": (time(22), time(7))}, ["TelegramProbe"]]
    # An alert group the bot would leave: a warning, and neither /status nor the watch.
    started.clear()
    with caplog.at_level(logging.WARNING):
        await app.run(Settings(bot_token=TOKEN, database_url="", allowed_chat_ids=frozenset({CHAT_ID}),
                               alert_chat_id=ALERT_CHAT))
    assert started[0]["alert_chat_id"] is None
    assert [(r.getMessage(), r.problem) for r in caplog.records if hasattr(r, "problem")] == [
        ("/status and the watch on the watchdog are off",
         "ALERT_CHAT_ID must be one of ALLOWED_CHAT_IDS: the bot leaves every other group")]
