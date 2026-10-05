"""Retention: what entered the archive more than RETENTION_MONTHS ago (messages.stored_at, not the
Telegram date) leaves it every night, with every copy of its words: its edits, the quotes of it in
replies that stay, and the bot's answers built on it (bot_requests.oldest_source_at), which go the
way /forget_me takes them. Requests, reminders and names follow their own entry.

Runs as a task inside the collector (nestor.collector.app.run), also without HERMES_URL. The cutoff
comes from the database's clock, which wrote stored_at; the Pi's clock only has to agree with it.
Every batch is one short transaction, so a run can stop at any point and the next one goes on. Each
run is a row of maintenance_runs (numbers and codes only) for the watchdog and /status.

A purge cannot be undone, so it refuses to run when the clocks of the Pi and the database differ,
when messages are dated in the future, when the cutoff is less than 360 days back, or when one night
would remove more than 5000 messages and more than 15 % of all of them: that waits for --confirm.

    python -m nestor.retention                   # what a run now would remove; nothing is deleted
    python -m nestor.retention --at 2027-10-05   # ... on that day
    python -m nestor.retention --run             # run now (once a day; the nightly run counts)
    python -m nestor.retention --confirm         # allow what the next 7 days remove, however much
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import asyncpg

from .forget import erase_lock
from .promises import RETENTION_MONTHS

log = logging.getLogger("nestor.retention")

BATCH = 500
MIN_BATCH = 50                  # a slow batch halves, down to this
PAUSE_S = 0.1                   # between batches: the collector's writes go first
STATEMENT_TIMEOUT_S = 120       # a batch holds ERASE_LOCK this long at most; nestor.forget.ERASE_WAIT_S outlasts it
JOB = "retention"
CONFIRM_JOB = "retention_confirm"
MAX_SKEW = timedelta(minutes=10)            # Pi clock vs database clock
FUTURE_SLACK = timedelta(minutes=5)
MIN_CUTOFF_DAYS = 360                       # whatever RETENTION_MONTHS says, never less than this
GUARD_ROWS = 5000                           # a night that removes more than this ...
GUARD_SHARE = 0.15                          # ... and more than this share of all messages waits for --confirm
CONFIRM_DAYS = 7
CONFIRM_MARGIN = 1.10                       # what was confirmed may grow by 10 % before it is asked again


class RetentionRefused(Exception):
    """A guard stopped the run: clock_skew | future_rows | cutoff_invalid."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# A. Answers built on messages that now leave (bot_requests.oldest_source_at): the bot's archived
#    answer, the quotes of it in replies, the text in the conversation history. As nestor.forget.
ANSWERS = """
WITH expired AS (
    SELECT id, chat_id, answer_msg_ids FROM bot_requests
    WHERE oldest_source_at < $1 AND answer IS NOT NULL
    ORDER BY oldest_source_at LIMIT $2
    FOR UPDATE SKIP LOCKED),
gone AS (
    DELETE FROM messages m USING expired e, users u
    WHERE m.chat_id = e.chat_id AND m.message_id = ANY(e.answer_msg_ids) AND u.user_id = m.user_id AND u.is_bot
    RETURNING 1),
unquoted AS (
    UPDATE messages r SET raw = r.raw - 'quote'
    FROM expired e
    WHERE r.chat_id = e.chat_id AND r.reply_to_id = ANY(e.answer_msg_ids) AND r.raw ? 'quote'
      AND NOT r.message_id = ANY(e.answer_msg_ids)
    RETURNING 1),
scrubbed AS (
    UPDATE bot_requests b SET answer = NULL FROM expired e WHERE b.id = e.id RETURNING 1)
SELECT (SELECT count(*) FROM scrubbed) AS answers, (SELECT count(*) FROM gone) AS answer_messages,
       (SELECT count(*) FROM unquoted) AS answer_quotes"""

# B. The messages themselves, oldest first; their edits go by ON DELETE CASCADE. A reply that
#    stays loses its quote of them (found by the replied id, so imported replies too). The span of
#    ids that left is remembered per chat, so the importer does not bring it back.
MESSAGES = """
WITH batch AS (
    SELECT chat_id, message_id FROM messages
    WHERE stored_at < $1
    ORDER BY stored_at LIMIT $2
    FOR UPDATE SKIP LOCKED),
edits AS (
    SELECT count(*) AS n FROM message_edits e JOIN batch b USING (chat_id, message_id)),
unquoted AS (
    UPDATE messages r SET raw = r.raw - 'quote'
    FROM batch b
    WHERE r.chat_id = b.chat_id AND r.reply_to_id = b.message_id AND r.raw ? 'quote' AND r.stored_at >= $1
    RETURNING 1),
gone AS (
    DELETE FROM messages m USING batch b
    WHERE m.chat_id = b.chat_id AND m.message_id = b.message_id
    RETURNING m.chat_id, m.message_id),
marked AS (
    INSERT INTO retention_marks AS k (chat_id, first_message_id, message_id)
    SELECT chat_id, min(message_id), max(message_id) FROM gone GROUP BY chat_id
    ON CONFLICT (chat_id) DO UPDATE
      SET first_message_id = least(k.first_message_id, EXCLUDED.first_message_id),
          message_id = greatest(k.message_id, EXCLUDED.message_id), updated_at = now()
    RETURNING 1)
SELECT (SELECT count(*) FROM gone) AS messages, (SELECT n FROM edits) AS edits,
       (SELECT count(*) FROM unquoted) AS quotes"""

# C. The log of requests, 12 months after the request; follow-ups keep their row (parent_id SET NULL).
REQUESTS = """
WITH old AS (
    SELECT id FROM bot_requests WHERE created_at < $1 ORDER BY created_at LIMIT $2 FOR UPDATE SKIP LOCKED),
gone AS (DELETE FROM bot_requests b USING old o WHERE b.id = o.id RETURNING 1)
SELECT count(*) AS requests FROM gone"""

# D. Names no message needs any more; the opt-out record and pending reminders keep theirs.
USERS = """
WITH idle AS (
    SELECT u.user_id FROM users u
    WHERE NOT u.opted_out AND u.updated_at < $1
      AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.user_id = u.user_id)
      AND NOT EXISTS (SELECT 1 FROM reminders r
                      WHERE r.user_id = u.user_id AND r.sent_at IS NULL AND r.cancelled_at IS NULL)
    LIMIT $2
    FOR UPDATE SKIP LOCKED),
gone AS (DELETE FROM users u USING idle i WHERE u.user_id = i.user_id RETURNING 1)
SELECT count(*) AS users FROM gone"""

BATCHED = (("answers", ANSWERS), ("messages", MESSAGES), ("requests", REQUESTS), ("users", USERS))

# E. Small tables, one statement each, on the database's now(); whether $1 (the cutoff) is used.
#    A reminder set a year ahead (reminders.MAX_AHEAD) stays until it fires; one stuck goes.
SINGLE = (
    ("reminders", True, """DELETE FROM reminders WHERE created_at < $1
                           AND (sent_at IS NOT NULL OR cancelled_at IS NOT NULL OR due_at < now() - interval '1 day')"""),
    ("daily_posts", True, "DELETE FROM daily_posts WHERE created_at < $1"),
    ("tickets", False, "DELETE FROM request_tickets WHERE expires_at < now()"),
    ("runs", True, "DELETE FROM maintenance_runs WHERE started_at < $1"),
)

PREVIEW = """
SELECT (SELECT count(*) FROM bot_requests WHERE oldest_source_at < $1 AND answer IS NOT NULL) AS answers,
       (SELECT count(*) FROM messages WHERE stored_at < $1) AS messages,
       (SELECT count(*) FROM message_edits e JOIN messages m USING (chat_id, message_id) WHERE m.stored_at < $1) AS edits,
       (SELECT count(*) FROM bot_requests WHERE created_at < $1) AS requests,
       (SELECT count(*) FROM reminders WHERE created_at < $1
          AND (sent_at IS NOT NULL OR cancelled_at IS NOT NULL OR due_at < $2::timestamptz - interval '1 day')) AS reminders,
       (SELECT count(*) FROM users u WHERE NOT u.opted_out AND u.updated_at < $1
          AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.user_id = u.user_id AND m.stored_at >= $1)
          AND NOT EXISTS (SELECT 1 FROM reminders r
                          WHERE r.user_id = u.user_id AND r.sent_at IS NULL AND r.cancelled_at IS NULL)) AS users,
       (SELECT count(*) FROM messages) AS total_messages"""

# What should have gone by now, with some grace: $1 = cutoff - grace, $2 = now - grace. An answer gets
# the grace from when it was logged too: a follow-up takes its parent's sources, so it can be logged
# already past the cutoff, and the next night takes it.
OVERDUE = """
SELECT (SELECT count(*) FROM messages WHERE stored_at < $1) AS messages,
       (SELECT count(*) FROM bot_requests WHERE oldest_source_at < $1 AND created_at < $2 AND answer IS NOT NULL)
         AS answers,
       (SELECT count(*) FROM bot_requests WHERE created_at < $1) AS requests,
       (SELECT count(*) FROM reminders WHERE created_at < $1
          AND (sent_at < $2 OR cancelled_at < $2 OR due_at < $2::timestamptz - interval '1 day')) AS reminders,
       (SELECT count(*) FROM users u WHERE NOT u.opted_out AND u.updated_at < $1
          AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.user_id = u.user_id)
          AND NOT EXISTS (SELECT 1 FROM reminders r
                          WHERE r.user_id = u.user_id AND r.sent_at IS NULL AND r.cancelled_at IS NULL)) AS users"""


def utcnow() -> datetime:
    return datetime.now(UTC)


async def db_times(conn, at: datetime | None = None) -> tuple[datetime, datetime]:
    """The database's time (or `at`) and the cutoff: RETENTION_MONTHS calendar months before it,
    as PostgreSQL counts them (29 Feb - 12 months = 28 Feb)."""
    row = await conn.fetchrow(
        """SELECT t AS now, t - make_interval(months => $2) AS cutoff
           FROM (SELECT coalesce($1::timestamptz, clock_timestamp()) AS t) s""", at, RETENTION_MONTHS)
    return row["now"], row["cutoff"]


async def preview(conn, at: datetime | None = None) -> dict[str, int]:
    """What a run at `at` (default: now, by the database's clock) would remove; deletes nothing."""
    now, cutoff = await db_times(conn, at)
    return dict(await conn.fetchrow(PREVIEW, cutoff, now))


async def remaining(conn, cutoff: datetime) -> int:
    return await conn.fetchval("SELECT count(*) FROM messages WHERE stored_at < $1", cutoff)


async def overdue(conn, grace: timedelta = timedelta(days=2)) -> dict[str, int]:
    """For the watchdog: per kind, what should have left the archive `grace` ago and is still here."""
    now, cutoff = await db_times(conn)
    return dict(await conn.fetchrow(OVERDUE, cutoff - grace, now - grace))


async def check_guards(conn, pi_now: datetime, db_now: datetime, cutoff: datetime) -> None:
    """Raises RetentionRefused when a clock cannot be trusted or the cutoff makes no sense."""
    if abs(pi_now - db_now) > MAX_SKEW:
        raise RetentionRefused("clock_skew")
    newest = await conn.fetchval("SELECT max(stored_at) FROM messages")
    if newest is not None and newest > db_now + FUTURE_SLACK:
        raise RetentionRefused("future_rows")
    if cutoff > db_now - timedelta(days=MIN_CUTOFF_DAYS):
        raise RetentionRefused("cutoff_invalid")


async def confirm(conn, day: date, ahead: timedelta = timedelta(days=CONFIRM_DAYS)) -> int:
    """An operator allows a large purge: records how many messages the runs of the next `ahead`
    would remove together, by the database's clock. Returns that number."""
    now, _ = await db_times(conn)
    count = (await preview(conn, now + ahead))["messages"]
    await conn.execute(
        """INSERT INTO maintenance_runs (job, day, status, counts, finished_at) VALUES ($1, $2, 'ok', $3, now())
           ON CONFLICT (job, day) DO UPDATE
             SET counts = EXCLUDED.counts, attempts = maintenance_runs.attempts + 1, updated_at = now(),
                 finished_at = now()""", CONFIRM_JOB, day, {"messages": count})
    return count


async def confirmed(conn, today: date, would_delete: int) -> bool:
    """Whether an operator allowed this much within the last CONFIRM_DAYS days."""
    rows = await conn.fetch("SELECT counts FROM maintenance_runs WHERE job = $1 AND day BETWEEN $2 AND $3",
                            CONFIRM_JOB, today - timedelta(days=CONFIRM_DAYS), today)
    return any(int(r["counts"].get("messages", 0)) * CONFIRM_MARGIN >= would_delete for r in rows)


async def claim(conn, day: date, cutoff: datetime) -> asyncpg.Record | None:
    """(id, cutoff, counts) if this call may run the day's purge: the first time that day, a failed
    run after 10 minutes (6 attempts a day), a blocked one after 10 minutes (it may be confirmed
    by now), or one that stopped mid-way (no batch for 15 minutes) with the counts it reached."""
    return await conn.fetchrow(
        """INSERT INTO maintenance_runs (job, day, status, cutoff) VALUES ($1, $2, 'running', $3)
           ON CONFLICT (job, day) DO UPDATE
             SET status = 'running', attempts = maintenance_runs.attempts + 1, cutoff = EXCLUDED.cutoff,
                 error = NULL, updated_at = now(), finished_at = NULL,
                 -- a blocked run removed nothing: its counts were what it would have removed
                 counts = CASE WHEN maintenance_runs.status = 'blocked' THEN '{}' ELSE maintenance_runs.counts END
             WHERE (maintenance_runs.status = 'failed' AND maintenance_runs.attempts < 6
                    AND maintenance_runs.updated_at < now() - interval '10 minutes')
                OR (maintenance_runs.status = 'blocked' AND maintenance_runs.updated_at < now() - interval '10 minutes')
                OR (maintenance_runs.status = 'running' AND maintenance_runs.updated_at < now() - interval '15 minutes')
           RETURNING id, cutoff, counts""", JOB, day, cutoff)


async def finish(conn, run_id: int, status: str, counts: dict | None, error: str | None = None) -> None:
    await conn.execute(
        """UPDATE maintenance_runs SET status = $2, counts = coalesce($3, counts), error = $4,
                                       finished_at = now(), updated_at = now()
           WHERE id = $1""", run_id, status, counts, error)


def _counts(total: Counter, slowest: int) -> dict[str, int]:
    return {k: v for k, v in total.items() if v} | ({"max_batch_ms": slowest} if slowest else {})


async def purge(pool: asyncpg.Pool, cutoff: datetime, *, run_id: int | None = None, counts: dict | None = None,
                batch: int = BATCH, pause_s: float = PAUSE_S) -> dict[str, int]:
    """Everything that entered the archive before `cutoff`, in batches. The counts (with those of
    earlier attempts of the same run) are written in the transaction of each batch, so they stay
    exact when the run is stopped. A batch slower than STATEMENT_TIMEOUT_S is cancelled and the
    batches halve. Safe to stop and to run again."""
    total = Counter({k: v for k, v in (counts or {}).items() if k != "max_batch_ms"})
    slowest = int((counts or {}).get("max_batch_ms", 0))
    size, misses = batch, 0

    async def step(name: str, sql: str, args: tuple, *, batched: bool) -> dict[str, int]:
        nonlocal slowest
        async with pool.acquire() as conn, conn.transaction():
            await erase_lock(conn)
            started = time.monotonic()
            if batched:
                got = dict(await conn.fetchrow(sql, *args, timeout=STATEMENT_TIMEOUT_S))
                took = int((time.monotonic() - started) * 1000)
            else:
                got = {name: int((await conn.execute(sql, *args, timeout=STATEMENT_TIMEOUT_S)).split()[-1])}
                took = 0
            after = total + Counter(got)
            if run_id is not None:
                await conn.execute("UPDATE maintenance_runs SET counts = $2, updated_at = now() WHERE id = $1",
                                   run_id, _counts(after, max(slowest, took)))
        total.update(got)
        slowest = max(slowest, took)
        return got

    for name, sql in BATCHED:
        while True:
            try:
                got = await step(name, sql, (cutoff, size), batched=True)
            except TimeoutError:
                misses = misses + 1 if size == MIN_BATCH else 0
                if misses >= 5:
                    raise
                size = max(MIN_BATCH, size // 2)
                log.warning("retention batch slow", extra={"step": name, "batch": size})
                continue
            misses = 0
            if not got[name]:
                break
            await asyncio.sleep(pause_s)
    for name, uses_cutoff, sql in SINGLE:
        await step(name, sql, (cutoff,) if uses_cutoff else (), batched=False)
    return _counts(total, slowest)


class Retention:
    """The nightly run, as its own task in the collector (started also without HERMES_URL)."""

    def __init__(self, pool: asyncpg.Pool, *, tz: ZoneInfo, at: dtime = dtime(3, 30), tick_s: float = 300,
                 batch: int = BATCH, pause_s: float = PAUSE_S, clock: Callable[[], datetime] = utcnow):
        self.pool, self.tz, self.at, self.tick_s = pool, tz, at, tick_s
        self.batch, self.pause_s, self.clock = batch, pause_s, clock

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self.tick_s)

    async def tick(self) -> str | None:
        from .collector.app import _TRANSIENT

        try:
            return await self.run_once(self.clock())
        except _TRANSIENT as e:     # database down: a later tick claims the run again
            log.warning("retention postponed", extra={"error": type(e).__name__})
        except Exception as e:  # noqa: BLE001 - never let a bug stop the loop; the class only, never its text
            log.error("retention failed", extra={"error": type(e).__name__, "sqlstate": getattr(e, "sqlstate", None)})
        return None

    async def run_once(self, pi_now: datetime) -> str | None:
        """'ok', 'failed' or 'blocked' if a run happened, None if it was not due or already done today."""
        local = pi_now.astimezone(self.tz)
        if local.time() < self.at:
            return None
        async with self.pool.acquire() as conn:
            db_now, cutoff = await db_times(conn)
            claimed = await claim(conn, local.date(), cutoff)
        if claimed is None:
            return None
        run_id = claimed["id"]
        try:
            async with self.pool.acquire() as conn:
                try:
                    await check_guards(conn, pi_now, db_now, cutoff)
                except RetentionRefused as e:
                    await finish(conn, run_id, "failed", None, e.code)
                    log.warning("retention refused", extra={"error": e.code})
                    return "failed"
                p = await preview(conn, db_now)
                if (p["messages"] > GUARD_ROWS and p["messages"] > GUARD_SHARE * p["total_messages"]
                        and not await confirmed(conn, local.date(), p["messages"])):
                    await finish(conn, run_id, "blocked",
                                 {"would_delete": p["messages"], "total": p["total_messages"]}, "too_many")
                    log.warning("retention blocked", extra={"would_delete": p["messages"], "total": p["total_messages"]})
                    return "blocked"
            counts = await purge(self.pool, cutoff, run_id=run_id, counts=claimed["counts"], batch=self.batch,
                                 pause_s=self.pause_s)
            async with self.pool.acquire() as conn:
                left = await remaining(conn, cutoff)      # rows another transaction held (SKIP LOCKED)
                status = "ok" if left == 0 else "failed"
                await finish(conn, run_id, status, None, None if left == 0 else "rows_left")
        except Exception as e:
            async with self.pool.acquire() as conn:   # may fail too: then the claim goes stale, see claim()
                await finish(conn, run_id, "failed", None, type(e).__name__)
            raise
        log.info("retention done", extra={"status": status, "cutoff": cutoff.isoformat(), "counts": counts})
        return status


async def _main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    import os

    from dotenv import load_dotenv

    from .collector import store
    from .config import _env, _timezone
    from .logs import setup_logging

    parser = argparse.ArgumentParser(prog="python -m nestor.retention", description=__doc__.split("\n\n")[0])
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--at", type=date.fromisoformat, metavar="YYYY-MM-DD",
                      help="what a run on that day (local noon) would remove")
    what.add_argument("--run", action="store_true", help="run the purge now (once a day, with the guards)")
    what.add_argument("--confirm", action="store_true",
                      help=f"allow the runs of the next {CONFIRM_DAYS} days to remove what they would remove now")
    args = parser.parse_args(argv)
    load_dotenv()
    setup_logging(_env("LOG_LEVEL", "INFO"))
    try:
        dsn = os.environ["DATABASE_URL"]       # only the database, like nestor.migrate
    except KeyError:
        raise SystemExit("missing required environment variable DATABASE_URL") from None
    tz = ZoneInfo(_timezone())
    pool = await store.create_pool(dsn)
    try:
        now = utcnow()
        if args.run:
            status = await Retention(pool, tz=tz, at=dtime(0)).run_once(now)
            if status is None:
                print("nothing to do: today's run is done or still running")
                return
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT error, counts FROM maintenance_runs WHERE job = $1 AND day = $2",
                                          JOB, now.astimezone(tz).date())
            print(status, row["error"] or "", json.dumps(row["counts"]))
        elif args.confirm:
            async with pool.acquire() as conn:
                print(f"confirmed {await confirm(conn, now.astimezone(tz).date())}")
        else:
            at = datetime.combine(args.at, dtime(12), tz) if args.at else None
            async with pool.acquire() as conn:
                for k, v in (await preview(conn, at)).items():
                    print(f"{k}: {v}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
