"""Stage 4 retention: what entered the archive more than 12 months ago leaves it with every copy of
its words, in batches, once a night, by the database's clock; the counts go to maintenance_runs."""
import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import asyncpg

from nestor import forget, promises, retention
from nestor.bot.answerer import AnswerConfig
from nestor.collector import store
from nestor.config import Settings
from nestor.logs import JsonFormatter

from .factories import CHAT_ID
from .test_answerer import BOT_ID

MSK = ZoneInfo("Europe/Moscow")


async def _setup(conn):
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT_ID)
    for uid, is_bot in ((111, False), (222, False), (333, False), (BOT_ID, True)):
        await conn.execute("INSERT INTO users (user_id, first_name, is_bot, updated_at) "
                           "VALUES ($1, 'U', $2, now() - interval '2 years')", uid, is_bot)


async def _msg(conn, mid, uid, text, *, stored, sent="now()", reply_to=None, quote=None, imported=False):
    raw = {"message_id": mid, "text": text}
    if reply_to is not None:
        raw["reply_to_message"] = {"message_id": reply_to}       # imported replies have no "from"
    if quote is not None:
        raw["quote"] = {"text": quote, "position": 0}
    if imported:
        raw["import_source"] = "telegram_import"
    await conn.execute(
        f"""INSERT INTO messages (chat_id, message_id, user_id, sent_at, reply_to_id, text, raw, stored_at)
            VALUES ($1, $2, $3, {sent}, $4, $5, $6, now() - $7::text::interval)""",
        CHAT_ID, mid, uid, reply_to, text, raw, stored)


async def _many(conn, first, last, *, stored):
    await conn.execute(
        """INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw, stored_at)
           SELECT $1, g, 111, now(), 'x', '{}', now() - $4::text::interval FROM generate_series($2::int, $3::int) g""",
        CHAT_ID, first, last, stored)


def _plain(counts: dict) -> dict:
    """Counts without the timing, which depends on the machine."""
    return {k: v for k, v in counts.items() if k != "max_batch_ms"}


async def _purge(pool, conn, **kw):
    _, cutoff = await retention.db_times(conn)
    return _plain(await retention.purge(pool, cutoff, pause_s=0, **kw))


async def _now(conn) -> datetime:
    """The database's time: the Pi's clock agrees with it, so the guards let the run go."""
    return await conn.fetchval("SELECT clock_timestamp()")


async def _run(conn) -> dict:
    return dict(await conn.fetchrow("SELECT status, attempts, counts, error FROM maintenance_runs WHERE job = $1",
                                    retention.JOB))


async def test_messages_leave_by_the_time_they_entered_the_archive_with_edits_and_quotes(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "старое", stored="13 months", sent="now() - interval '13 months'")
    await _msg(conn, 2, 222, "история 2019, загружена 11 месяцев назад", stored="11 months",
               sent="'2019-01-01'", imported=True)
    await _msg(conn, 3, 333, "свежая дата, но в архиве больше года", stored="13 months")
    await _msg(conn, 4, 222, "ответ с цитатой старого", stored="6 months", reply_to=1, quote="старое", imported=True)
    await _msg(conn, 5, 111, "ответ с цитатой живого", stored="6 months", reply_to=2, quote="история")
    await conn.execute("INSERT INTO message_edits (chat_id, message_id, old_text, replaced_at) "
                       "VALUES ($1, 1, 'черновик', now())", CHAT_ID)
    got = await _purge(pool, conn, batch=1)
    assert got == {"messages": 2, "edits": 1, "quotes": 1, "users": 2}   # 333 and the bot: no messages left
    left = {r["message_id"]: r["raw"] for r in await conn.fetch("SELECT message_id, raw FROM messages")}
    assert sorted(left) == [2, 4, 5]
    assert "quote" not in left[4] and "старое" not in str(left[4])
    assert left[5]["quote"]["text"] == "история"
    assert await conn.fetchval("SELECT count(*) FROM message_edits") == 0
    span = await conn.fetchrow("SELECT first_message_id, message_id FROM retention_marks WHERE chat_id = $1", CHAT_ID)
    assert tuple(span) == (1, 3)                     # the ids that left: the importer does not bring them back
    assert await _purge(pool, conn) == {}            # nothing left to do


async def test_the_mark_keeps_the_newest_id_whatever_order_the_purge_goes_in(pool, conn):
    """The purge goes by stored_at, Telegram's ids by sent time: imported history has the lower ids and
    the later stored_at. The span must not shrink when a lower id leaves after a higher one, or the
    importer would load the messages between again, with a fresh stored_at."""
    await _setup(conn)
    await _msg(conn, 9, 111, "живое, давно в архиве", stored="14 months")
    await _msg(conn, 3, 222, "история, загружена позже", stored="13 months", imported=True)
    assert (await _purge(pool, conn, batch=1))["messages"] == 2        # 9 leaves first, then 3
    span = await conn.fetchrow("SELECT first_message_id, message_id FROM retention_marks WHERE chat_id = $1", CHAT_ID)
    assert tuple(span) == (3, 9)                     # the importer skips 3..9, not only 3..3


async def test_answers_built_on_purged_messages_go_the_way_forget_me_takes_them(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "старое", stored="13 months")
    await _msg(conn, 2, 222, "нестор, что тут было?", stored="1 month")
    await _msg(conn, 900, BOT_ID, "Было «старое»", stored="1 month", reply_to=2)
    await _msg(conn, 3, 333, "ответ боту", stored="20 days", reply_to=900, quote="Было «старое»")
    rid = await conn.fetchval(
        """INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, scenario, question, answer, status,
                                     answer_msg_ids, created_at, oldest_source_at)
           VALUES ($1, 2, 222, 'other', 'что тут было?', 'Было [«старое»](#1)', 'ok', '{900}',
                   now() - interval '1 month', now() - interval '13 months') RETURNING id""", CHAT_ID)
    got = await _purge(pool, conn)
    assert (got["answers"], got["answer_messages"], got["answer_quotes"], got["messages"]) == (1, 1, 1, 1)
    row = await conn.fetchrow("SELECT question, answer, answer_msg_ids FROM bot_requests WHERE id = $1", rid)
    # What /forget_me leaves: a reply to the answer still finds its conversation, without the text.
    assert (row["question"], row["answer"], row["answer_msg_ids"]) == ("что тут было?", None, [900])
    assert sorted(r[0] for r in await conn.fetch("SELECT message_id FROM messages")) == [2, 3]
    assert "quote" not in await conn.fetchval("SELECT raw FROM messages WHERE message_id = 3")


async def test_requests_reminders_and_names_follow_their_own_entry(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "живое", stored="1 day")
    old = await conn.fetchval("""INSERT INTO bot_requests (chat_id, user_id, scenario, status, created_at)
                                 VALUES ($1, 222, 'other', 'ok', now() - interval '13 months') RETURNING id""", CHAT_ID)
    child = await conn.fetchval("""INSERT INTO bot_requests (chat_id, user_id, scenario, status, parent_id)
                                   VALUES ($1, 222, 'other', 'ok', $2) RETURNING id""", CHAT_ID, old)
    for created, sent, due in (("13 months", "now()", "now()"),                         # fired: goes
                               ("12 months 1 day", None, "now() + interval '1 hour'"),  # set a year ahead: stays
                               ("13 months", None, "now() - interval '30 days'"),      # stuck: goes
                               ("1 month", "now()", "now()")):
        await conn.execute(f"""INSERT INTO reminders (chat_id, user_id, request_msg_id, text, due_at, created_at, sent_at)
                               VALUES ($1, 333, 1, 'купить хлеб', {due}, now() - $2::text::interval, {sent or 'NULL'})""",
                           CHAT_ID, created)
    await conn.execute("""INSERT INTO daily_posts (chat_id, kind, day, status, created_at)
                          VALUES ($1, 'wisdom', '2025-01-01', 'posted', now() - interval '13 months'),
                                 ($1, 'wisdom', '2026-01-01', 'posted', now())""", CHAT_ID)
    await conn.execute("""INSERT INTO request_tickets VALUES ('old', $1, NULL, now() - interval '1 minute'),
                                                            ('live', $1, NULL, now() + interval '1 minute')""", CHAT_ID)
    await conn.execute("""INSERT INTO maintenance_runs (job, day, status, started_at)
                          VALUES ('retention', '2025-01-01', 'ok', now() - interval '13 months')""")
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 222")
    got = await _purge(pool, conn)
    assert got == {"requests": 1, "reminders": 2, "users": 1, "daily_posts": 1, "tickets": 1, "runs": 1}
    assert await conn.fetchval("SELECT parent_id FROM bot_requests WHERE id = $1", child) is None
    # 111 has a message, 222 is the opt-out record, 333 waits for a reminder; the bot has nothing left.
    assert sorted(r[0] for r in await conn.fetch("SELECT user_id FROM users")) == [111, 222, 333]
    assert await conn.fetchval("SELECT count(*) FROM reminders WHERE sent_at IS NULL") == 1


async def test_the_nightly_run_claims_its_day_records_counts_and_resumes(pool, conn):
    await _setup(conn)
    for mid in range(1, 8):
        await _msg(conn, mid, 111, "x", stored="13 months")
    pi_now = await _now(conn)
    day = pi_now.astimezone(MSK).date()
    nightly = retention.Retention(pool, tz=MSK, at=dtime(3, 30))
    assert await nightly.run_once(datetime.combine(day, dtime(3, 0), MSK)) is None        # not yet
    assert await conn.fetchval("SELECT count(*) FROM maintenance_runs") == 0
    # A run stopped mid-way: two batches done, then the process died.
    _, cutoff = await retention.db_times(conn)
    run = await retention.claim(conn, day, cutoff)
    await conn.execute("""UPDATE maintenance_runs SET counts = '{"messages": 4}', updated_at = now() - interval '20 minutes'
                          WHERE id = $1""", run["id"])
    await conn.execute("DELETE FROM messages WHERE message_id <= 4")
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), batch=2, pause_s=0)
    assert await ret.run_once(pi_now) == "ok"
    assert await ret.run_once(pi_now) is None                                               # once a day
    row = await _run(conn)
    assert (row["status"], row["attempts"], _plain(row["counts"]), row["error"]) == \
        ("ok", 2, {"messages": 7, "users": 4}, None)
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_cutoff_comes_from_the_database_clock(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "почти год", stored="11 months")
    db_now = await _now(conn)
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), pause_s=0)
    # The Pi's clock jumped 400 days ahead: with its cutoff, the message would go.
    assert await ret.run_once(db_now + timedelta(days=400)) == "failed"
    row = await _run(conn)
    assert (row["status"], row["error"]) == ("failed", "clock_skew")
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    cutoff = await conn.fetchval("SELECT cutoff FROM maintenance_runs")
    assert abs(cutoff - await conn.fetchval("SELECT now() - interval '12 months'")) < timedelta(minutes=1)


async def test_rows_from_the_future_refuse_the_run(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "старое", stored="13 months")
    await _msg(conn, 2, 111, "из будущего", stored="-1 day")
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), pause_s=0)
    assert await ret.run_once(await _now(conn)) == "failed"
    assert (await _run(conn))["error"] == "future_rows"
    assert await conn.fetchval("SELECT count(*) FROM messages") == 2


async def test_a_cutoff_less_than_360_days_back_is_refused(conn):
    db_now = await _now(conn)
    try:
        await retention.check_guards(conn, db_now, db_now, db_now - timedelta(days=359))
    except retention.RetentionRefused as e:
        assert e.code == "cutoff_invalid"
    else:
        raise AssertionError("a cutoff 359 days back must be refused")
    await retention.check_guards(conn, db_now, db_now, db_now - timedelta(days=365))


async def test_a_cliff_is_blocked_until_confirmed(pool, conn):
    await _setup(conn)
    await _many(conn, 1, 6000, stored="13 months")
    await _many(conn, 6001, 9000, stored="1 day")
    await _many(conn, 9001, 9500, stored="12 months -3 days")      # leave in 3 days: confirm counts them too
    pi_now = await _now(conn)
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), pause_s=0)
    assert await ret.run_once(pi_now) == "blocked"
    row = await _run(conn)
    assert (row["status"], row["counts"], row["error"]) == ("blocked", {"would_delete": 6000, "total": 9500}, "too_many")
    assert await conn.fetchval("SELECT count(*) FROM messages") == 9500
    assert await ret.run_once(pi_now) is None                   # asked again only after 10 minutes

    assert await retention.confirm(conn, pi_now.astimezone(MSK).date()) == 6500
    await conn.execute("UPDATE maintenance_runs SET updated_at = now() - interval '11 minutes' WHERE job = $1",
                       retention.JOB)
    assert await ret.run_once(pi_now) == "ok"
    row = await _run(conn)
    assert (row["status"], row["error"], row["counts"]["messages"]) == ("ok", None, 6000)
    assert "would_delete" not in row["counts"]
    assert await conn.fetchval("SELECT count(*) FROM messages") == 3500


async def test_confirm_does_not_cover_more_than_it_saw(pool, conn):
    await _setup(conn)
    await _many(conn, 1, 6000, stored="13 months")
    await _many(conn, 6001, 9000, stored="1 day")
    pi_now = await _now(conn)
    day = pi_now.astimezone(MSK).date()
    assert await retention.confirm(conn, day) == 6000
    assert await retention.confirmed(conn, day, 6600) and not await retention.confirmed(conn, day, 6601)
    assert not await retention.confirmed(conn, day + timedelta(days=8), 10)     # a week later it is asked again
    await _many(conn, 9001, 10000, stored="13 months")                          # 7000 now: more than 6000 + 10 %
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), pause_s=0)
    assert await ret.run_once(pi_now) == "blocked"
    assert (await _run(conn))["counts"] == {"would_delete": 7000, "total": 10000}
    assert await conn.fetchval("SELECT count(*) FROM messages") == 10000


async def test_a_busy_month_that_is_a_small_share_of_the_archive_runs_without_confirm(pool, conn):
    await _setup(conn)
    await _many(conn, 1, 6000, stored="13 months")          # more than 5000, but 10 % of 60 000
    await _many(conn, 6001, 60000, stored="1 day")
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), pause_s=0)
    assert await ret.run_once(await _now(conn)) == "ok"
    row = await _run(conn)
    assert (row["status"], row["error"], row["counts"]["messages"]) == ("ok", None, 6000)
    assert await conn.fetchval("SELECT count(*) FROM messages") == 54000


class _SlowPool:
    """The pool, but the first `times` batch statements time out, like a slow router on the night of the cliff."""

    def __init__(self, pool, times=1):
        self.pool, self.sizes, self.left = pool, [], times

    @contextlib.asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            yield _SlowConn(conn, self)


class _SlowConn:
    def __init__(self, conn, owner):
        self._conn, self._owner = conn, owner

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchrow(self, sql, *args, **kw):
        if "LIMIT $2" in sql:                   # a batch: $2 is its size
            self._owner.sizes.append(args[1])
            assert len(self._owner.sizes) < 50, "the purge never gives up"      # ends a runaway loop
            await asyncio.sleep(0.005)
            if self._owner.left:
                self._owner.left -= 1
                raise TimeoutError
        return await self._conn.fetchrow(sql, *args, **kw)


async def test_slow_batch_halves_and_continues(pool, conn, caplog):
    await _setup(conn)
    await _many(conn, 1, 600, stored="13 months")
    slow = _SlowPool(pool)
    ret = retention.Retention(slow, tz=MSK, at=dtime(0), pause_s=0)
    with caplog.at_level(logging.WARNING, logger="nestor.retention"):
        assert await ret.run_once(await _now(conn)) == "ok"
    assert slow.sizes[:2] == [500, 250] and set(slow.sizes[1:]) == {250}
    assert [(r.getMessage(), r.step, r.batch) for r in caplog.records if r.name == "nestor.retention"] == \
        [("retention batch slow", "answers", 250)]
    row = await _run(conn)
    assert row["counts"]["messages"] == 600 and row["counts"]["max_batch_ms"] >= 5
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_a_batch_that_stays_slow_postpones_the_run_to_a_later_tick(pool, conn, caplog):
    await _setup(conn)
    await _many(conn, 1, 10, stored="13 months")
    pi_now = await _now(conn)
    slow = _SlowPool(pool, times=10**6)
    ret = retention.Retention(slow, tz=MSK, at=dtime(0), pause_s=0, clock=lambda: pi_now)
    with caplog.at_level(logging.WARNING, logger="nestor.retention"):
        assert await ret.tick() is None
    # It halves down to MIN_BATCH, tries that five times, and gives the connection back.
    assert slow.sizes == [500, 250, 125, 62] + [retention.MIN_BATCH] * 5
    row = await _run(conn)
    assert (row["status"], row["error"]) == ("failed", "TimeoutError")
    logged = [r.getMessage() for r in caplog.records if r.name == "nestor.retention"]
    assert "retention postponed" in logged and "retention failed" not in logged
    assert await conn.fetchval("SELECT count(*) FROM messages") == 10
    slow.left = 0                                       # the router is itself again
    await conn.execute("UPDATE maintenance_runs SET updated_at = now() - interval '11 minutes'")
    assert await ret.tick() == "ok"
    assert (await _run(conn))["attempts"] == 2 and await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_rows_another_transaction_holds_make_the_run_fail_and_are_taken_next_time(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "занято", stored="13 months")
    await _msg(conn, 2, 111, "свободно", stored="13 months")
    pi_now = await _now(conn)
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), pause_s=0)
    async with pool.acquire() as other:
        holding = other.transaction()
        await holding.start()
        await other.execute("SELECT 1 FROM messages WHERE message_id = 1 FOR UPDATE")      # SKIP LOCKED passes it by
        assert await ret.run_once(pi_now) == "failed"
        await holding.rollback()
    row = await _run(conn)
    assert (row["status"], row["error"], row["counts"]["messages"]) == ("failed", "rows_left", 1)
    assert await conn.fetchval("SELECT message_id FROM messages") == 1
    await conn.execute("UPDATE maintenance_runs SET updated_at = now() - interval '11 minutes'")
    assert await ret.run_once(pi_now) == "ok"
    row = await _run(conn)
    assert (row["status"], row["attempts"], row["error"], row["counts"]["messages"]) == ("ok", 2, None, 2)
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_a_run_that_failed_six_times_is_not_claimed_again_that_day(conn):
    day = (await _now(conn)).astimezone(MSK).date()
    _, cutoff = await retention.db_times(conn)
    await conn.execute("""INSERT INTO maintenance_runs (job, day, status, attempts, updated_at)
                          VALUES ($1, $2, 'failed', 6, now() - interval '1 hour')""", retention.JOB, day)
    assert await retention.claim(conn, day, cutoff) is None
    await conn.execute("UPDATE maintenance_runs SET attempts = 5")
    assert await retention.claim(conn, day, cutoff) is not None


async def test_purge_waits_for_forget_me(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "старое", stored="13 months")
    _, cutoff = await retention.db_times(conn)
    async with pool.acquire() as other:
        forgetting = other.transaction()
        await forgetting.start()
        await forget.erase_lock(other)                  # a /forget_me in flight
        purging = asyncio.create_task(retention.purge(pool, cutoff, pause_s=0))
        await asyncio.sleep(0.3)
        assert not purging.done()
        assert await conn.fetchval("SELECT count(*) FROM messages") == 1
        await forgetting.commit()
    assert _plain(await purging)["messages"] == 1
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_purge_waits_for_a_forget_me_that_has_started(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 222, "старое слово Пети", stored="13 months")
    await _msg(conn, 2, 111, "ответ", stored="1 day", reply_to=1, quote="старое")
    _, cutoff = await retention.db_times(conn)
    async with pool.acquire() as other:
        forgetting = other.transaction()
        await forgetting.start()
        await forget.forget(other, 222, BOT_ID)         # it holds its rows now: the reply (quote) and the old message
        purging = asyncio.create_task(retention.purge(pool, cutoff, pause_s=0))
        await asyncio.sleep(0.3)
        assert not purging.done()
        await forgetting.commit()
    assert _plain(await purging).get("messages", 0) == 0     # the /forget_me took it
    assert [(r["message_id"], "quote" in r["raw"]) for r in await conn.fetch("SELECT message_id, raw FROM messages")] \
        == [(2, False)]


async def _waits_for_a_batch(pool, erasing) -> bool:
    """Whether `erasing(conn)`, run in its own transaction, waits while a purge batch holds ERASE_LOCK
    and then finishes."""
    async def erase():
        async with pool.acquire() as c, c.transaction():
            await erasing(c)

    async with pool.acquire() as batch:
        purging = batch.transaction()
        await purging.start()
        await forget.erase_lock(batch)                  # a batch in flight
        task = asyncio.create_task(erase())
        await asyncio.sleep(0.3)
        waiting = not task.done()
        await purging.commit()
    await task
    return waiting


async def test_forget_me_and_the_scrub_of_a_logged_answer_wait_for_a_batch_in_flight(pool, conn):
    await _setup(conn)
    await _msg(conn, 1, 222, "слово Пети", stored="1 day")
    rid = await conn.fetchval("""INSERT INTO bot_requests (chat_id, user_id, scenario, answer, status, quoted_user_ids)
                                 VALUES ($1, 111, 'other', 'Петя: «слово»', 'ok', '{222}') RETURNING id""", CHAT_ID)
    assert await _waits_for_a_batch(pool, lambda c: forget.forget(c, 222, BOT_ID))
    await conn.execute("UPDATE bot_requests SET answer = 'Петя: «слово»' WHERE id = $1", rid)
    assert await _waits_for_a_batch(pool, lambda c: forget.scrub_if_opted_out(c, rid, [222], BOT_ID))
    assert await conn.fetchval("SELECT answer FROM bot_requests WHERE id = $1", rid) is None


async def test_forget_me_waits_for_a_slow_purge_batch_instead_of_failing(database, pool, conn):
    """A purge batch may hold the lock for 120 s, the pool gives a query 30 s: here 0.6 s against 0.3 s."""
    assert forget.ERASE_WAIT_S > retention.STATEMENT_TIMEOUT_S + 30
    await _setup(conn)
    short = await asyncpg.create_pool(database["collector"], min_size=1, max_size=1, init=store.init_connection,
                                      command_timeout=0.3)

    async def forgetting():
        async with short.acquire() as c, c.transaction():
            return await forget.forget(c, 222, BOT_ID)

    try:
        async with pool.acquire() as batch:
            purging = batch.transaction()
            await purging.start()
            await forget.erase_lock(batch)                  # a batch in flight
            task = asyncio.create_task(forgetting())
            await asyncio.sleep(0.6)
            assert not task.done()
            await purging.commit()
        await task
    finally:
        await short.close()
    assert await conn.fetchval("SELECT opted_out FROM users WHERE user_id = 222")


async def test_preview_counts_without_deleting(conn):
    await _setup(conn)
    await _msg(conn, 1, 111, "старое", stored="13 months")
    await _msg(conn, 2, 222, "тоже старое", stored="13 months")
    await _msg(conn, 3, 111, "свежее", stored="1 day")
    await _msg(conn, 4, 111, "уйдёт через 10 дней", stored="11 months 20 days")
    await conn.execute("INSERT INTO message_edits (chat_id, message_id, old_text, replaced_at) "
                       "VALUES ($1, 2, 'черновик', now())", CHAT_ID)
    await conn.execute("""INSERT INTO bot_requests (chat_id, user_id, scenario, answer, status, created_at, oldest_source_at)
                          VALUES ($1, 111, 'other', 'ответ', 'ok', now() - interval '1 day', now() - interval '13 months'),
                                 ($1, 111, 'other', NULL, 'ok', now() - interval '13 months', now() - interval '13 months')""",
                       CHAT_ID)
    await conn.execute("""INSERT INTO reminders (chat_id, user_id, request_msg_id, text, due_at, created_at, sent_at)
                          VALUES ($1, 111, 1, 'x', now(), now() - interval '13 months', now())""", CHAT_ID)
    expected = {"answers": 1, "messages": 2, "edits": 1, "requests": 1, "reminders": 1, "users": 3,
                "total_messages": 4}                 # 222, 333 and the bot keep no message newer than the cutoff
    assert await retention.preview(conn) == expected
    assert await retention.preview(conn) == expected     # nothing was deleted
    in_a_month = await conn.fetchval("SELECT now() + interval '1 month'")
    assert (await retention.preview(conn, in_a_month))["messages"] == 3        # a run on that day takes #4 too
    assert await conn.fetchval("SELECT count(*) FROM messages") == 4


async def test_the_cli_previews_a_day_confirms_and_runs(database, conn, monkeypatch, capsys):
    await _setup(conn)
    await _msg(conn, 1, 111, "уйдёт через 3 дня", stored="12 months -3 days")
    await _msg(conn, 2, 111, "свежее", stored="1 day")
    monkeypatch.setenv("DATABASE_URL", database["collector"])
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)        # keeps pytest's log capture
    today = (await _now(conn)).astimezone(MSK).date()

    async def cli(*args) -> list[str]:
        await retention._main(list(args))
        return capsys.readouterr().out.splitlines()

    assert "messages: 0" in await cli()                                     # now: nothing is due
    assert "messages: 1" in await cli("--at", (today + timedelta(days=10)).isoformat())
    assert "messages: 0" in await cli("--at", today.isoformat())
    assert await cli("--confirm") == ["confirmed 1"]                        # the next 7 days take #1
    assert await conn.fetchval("SELECT counts FROM maintenance_runs WHERE job = $1",
                               retention.CONFIRM_JOB) == {"messages": 1}
    assert await conn.fetchval("SELECT count(*) FROM messages") == 2        # nothing above deleted anything
    assert (await cli("--run"))[0].startswith("ok ")
    assert await cli("--run") == ["nothing to do: today's run is done or still running"]


async def test_overdue_counts_every_kind(conn):
    await _setup(conn)
    late, fresh = "12 months 3 days", "12 months 1 day"      # grace is 2 days
    await _msg(conn, 1, 111, "давно должно было уйти", stored=late)
    await _msg(conn, 2, 111, "уйдёт сегодня ночью", stored=fresh)
    await conn.execute(f"""INSERT INTO bot_requests (chat_id, user_id, scenario, answer, status, created_at, oldest_source_at)
                           VALUES ($1, 111, 'other', 'ответ', 'ok', now() - interval '3 days', now() - interval '{late}'),
                                  ($1, 111, 'other', 'ответ', 'ok', now() - interval '3 days', now() - interval '{fresh}'),
                                  ($1, 111, 'other', NULL, 'ok', now() - interval '{late}', now() - interval '{late}'),
                                  -- a follow-up to an old answer, logged today: the next night takes it
                                  ($1, 111, 'other', 'ответ', 'ok', now() - interval '1 day', now() - interval '{late}')""",
                       CHAT_ID)
    await conn.execute(f"""INSERT INTO reminders (chat_id, user_id, request_msg_id, text, due_at, created_at, sent_at)
                           VALUES ($1, 111, 1, 'x', now(), now() - interval '{late}', now() - interval '3 days'),
                                  ($1, 111, 1, 'x', now(), now() - interval '{late}', now() - interval '1 hour')""",
                       CHAT_ID)
    await conn.execute("UPDATE users SET updated_at = now() - interval '13 months'")
    # 222, 333 and the bot have no messages and nothing pending: their names are overdue.
    assert await retention.overdue(conn) == {"messages": 1, "answers": 1, "requests": 1, "reminders": 1, "users": 3}


def test_promised_numbers():
    assert promises.RETENTION_MONTHS == 12
    assert promises.BACKUP_WEEKS == -(-promises.BACKUP_MAX_AGE_DAYS // 7) == 5
    assert AnswerConfig().retention_months == 12


async def test_retention_starts_without_hermes(monkeypatch):
    from nestor.collector import app

    events = []

    async def run(self):
        events.append(("started", self.at, self.tz.key))
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    class NoPool:
        async def close(self):
            events.append("pool closed")

    async def create_pool(dsn):
        return NoPool()

    async def start_polling(self, bot, **kw):
        await asyncio.sleep(0)                      # the task gets to run
        events.append("polling")

    monkeypatch.setattr(retention.Retention, "run", run)
    monkeypatch.setattr(app.store, "create_pool", create_pool)
    monkeypatch.setattr(app.Dispatcher, "start_polling", start_polling)
    await app.run(Settings(bot_token="42:TEST", database_url="", allowed_chat_ids=frozenset({CHAT_ID})))
    assert events == [("started", dtime(3, 30), "Europe/Moscow"), "polling", "stopped", "pool closed"]


async def test_failed_run_is_retried_after_ten_minutes_and_logged_without_text(pool, conn, monkeypatch, caplog):
    canary = "КАНАРЕЙКА секретный текст"
    await _setup(conn)
    await _msg(conn, 1, 111, canary, stored="13 months")
    pi_now = await _now(conn)
    real_purge = retention.purge

    async def broken(*args, **kw):          # PostgreSQL puts the row into the error: «Failing row contains (...)»
        raise asyncpg.CheckViolationError(f"new row violates check constraint\nDETAIL: Failing row contains ({canary})")

    monkeypatch.setattr(retention, "purge", broken)
    ret = retention.Retention(pool, tz=MSK, at=dtime(0), pause_s=0, clock=lambda: pi_now)
    with caplog.at_level(logging.INFO):
        assert await ret.tick() is None
        row = await _run(conn)
        assert (row["status"], row["error"], row["attempts"]) == ("failed", "CheckViolationError", 1)
        assert await ret.tick() is None                     # not again before 10 minutes
        assert (await _run(conn))["attempts"] == 1
        monkeypatch.setattr(retention, "purge", real_purge)
        await conn.execute("UPDATE maintenance_runs SET updated_at = now() - interval '11 minutes'")
        assert await ret.tick() == "ok"
    row = await _run(conn)
    assert (row["status"], row["error"], row["attempts"], row["counts"]["messages"]) == ("ok", None, 2, 1)
    failed = [r for r in caplog.records if r.getMessage() == "retention failed"]
    assert [(r.error, r.sqlstate) for r in failed] == [("CheckViolationError", "23514")]
    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "retention done" in formatted and "КАНАРЕЙКА" not in formatted and "КАНАРЕЙКА" not in caplog.text
