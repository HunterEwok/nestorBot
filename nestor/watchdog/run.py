"""One run of the watchdog, as the timer starts it every 5 minutes:

1. write and fsync a probe file on the card (sd.write);
2. connect to the database (10 s);
3. run the checks that are due, each at most 20 s, in a read-only transaction;
4. evaluate them against the state of the earlier runs (nestor.watchdog.alerts);
5. the daily summary, if it is due;
6. send;
7. the snapshot for /status into ops_state('watchdog'), in its own short transaction, with the state:
   the next run takes it from there if this one cannot save it (alerts.adopt);
8. the opted-out user ids into optouts.json (0600): a restore from a backup forgets them again;
9. save the state, on the card and in the unit's RuntimeDirectory (memory, lost on reboot): a card that
   went read-only while the database is down still lets the next run know what this one said.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from aiogram import Bot

from ..collector import store
from ..config import Settings
from ..forget import SHARED_IDS
from . import alerts, notify
from .alerts import AlertState
from .checks import (
    CHECKS,
    Check,
    CheckResult,
    Ctx,
    broken,
    card_result,
    db_result,
    unknown,
)
from .probes import Host, run_systemctl
from .summary import daily_summary

log = logging.getLogger("nestor.watchdog")

STATE_DIR = Path("/var/lib/nestor-watchdog")      # the unit's StateDirectory
RUNTIME_DIR = Path("/run/nestor-watchdog")        # its RuntimeDirectory
STATE_FILE = "state.json"
OPTOUTS_FILE = "optouts.json"
CHECK_TIMEOUT_S = 20
DB_TIMEOUT_S = 10
SLACK = timedelta(minutes=1)        # the timer's accuracy: a 15-minute check runs on every third run


class Busy(Exception):
    """Another run holds the state."""


@dataclass
class Report:
    results: list[CheckResult]
    snapshot: dict
    message: alerts.Outgoing | None = None
    summary: str | None = None
    sent: bool = False


def lock(state_dir: Path, *, wait: bool = False) -> int | None:
    """An flock on the state directory itself, which works on a read-only card too. None when there is no
    directory: then there is no state to keep apart either."""
    try:
        fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd)
        raise Busy from None
    return fd


async def connect(url: str) -> tuple[asyncpg.Connection | None, datetime | None, str | None]:
    """(connection, the database's clock, None), or (None, None, the error's class name)."""
    try:
        conn = await asyncpg.connect(url, timeout=DB_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - the class only: its text can name the host and the role
        return None, None, type(e).__name__
    try:
        await store.init_connection(conn)
        return conn, await conn.fetchval("SELECT clock_timestamp()", timeout=DB_TIMEOUT_S), None
    except Exception as e:  # noqa: BLE001
        conn.terminate()
        return None, None, type(e).__name__


def load_state(state_dir: Path, runtime_dir: Path | None) -> AlertState:
    """The newer of the state on the card and its copy in memory."""
    states = [alerts.load_state(state_dir / STATE_FILE)]
    if runtime_dir is not None and (runtime_dir / STATE_FILE).exists():
        states.append(alerts.load_state(runtime_dir / STATE_FILE))
    oldest = datetime.min.replace(tzinfo=UTC)
    return max(states, key=lambda s: s.saved_at or oldest)


def keep_in_memory(runtime_dir: Path | None, state: AlertState, now: datetime) -> None:
    """The copy of the state in the RuntimeDirectory, if there is one."""
    if runtime_dir is not None and runtime_dir.is_dir():
        try:
            alerts.save_state(runtime_dir / STATE_FILE, state, now)
        except OSError as e:
            log.warning("watchdog state not kept in memory", extra={"error": type(e).__name__})


def due(check: Check, state: AlertState, now: datetime) -> bool:
    last = state.ran.get(check.name)
    return last is None or last > now or now - last >= check.every - SLACK


async def run_check(ctx: Ctx, check: Check) -> CheckResult:
    """One check: in a read-only transaction while the database is there, at most CHECK_TIMEOUT_S. A check
    that raises is a problem of its own (watchdog.check:<name>); its other problems stay as they were."""
    if check.needs_db and ctx.conn is None:
        return unknown(check.name)

    async def call() -> CheckResult:
        if ctx.conn is None:
            return await check.fn(ctx)
        async with ctx.conn.transaction(readonly=True):
            return await check.fn(ctx)

    try:
        return await asyncio.wait_for(call(), CHECK_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - one broken check must not stop the others
        timeout, conn = isinstance(e, TimeoutError), ctx.conn
        if conn is not None and (timeout or conn.is_closed() or conn.is_in_transaction()):
            conn.terminate()        # a query may still run on it: the rest of the run goes without it
            ctx.conn = None
            if not timeout:         # the database went away under it: not the check's fault
                log.warning("watchdog lost the database", extra={"check": check.name, "error": type(e).__name__})
                return unknown(check.name)
        log.warning("watchdog check failed", extra={"check": check.name, "error": type(e).__name__})
        return broken(check.name, type(e).__name__, confirm=2 if timeout else 1)


async def _summary(ctx: Ctx, bot: Bot) -> str | None:
    async def make() -> str | None:
        if ctx.conn is None:
            return await daily_summary(None, ctx, bot)
        async with ctx.conn.transaction(readonly=True):
            return await daily_summary(ctx.conn, ctx, bot)

    try:
        return await asyncio.wait_for(make(), CHECK_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - tried again on the next run
        log.warning("daily summary failed", extra={"error": type(e).__name__})
        return None


async def _publish(conn: asyncpg.Connection, snap: dict) -> None:
    try:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO ops_state (key, value, updated_at) VALUES ('watchdog', $1, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", snap)
    except Exception as e:  # noqa: BLE001 - /status says the snapshot is old; the alerts went out anyway
        log.warning("watchdog snapshot not saved", extra={"error": type(e).__name__})


async def keep_optouts(conn: asyncpg.Connection, state_dir: Path) -> None:
    """Adds the ids of everyone who used /forget_me to optouts.json; an id once there stays. Ids only, and
    never one of forget.SHARED_IDS, which are not one person."""
    path = state_dir / OPTOUTS_FILE
    try:
        async with conn.transaction(readonly=True):
            ids = {r["user_id"] for r in await conn.fetch("SELECT user_id FROM users WHERE opted_out")} - SHARED_IDS
        try:
            before = {i for i in json.loads(path.read_text(encoding="utf-8")) if isinstance(i, int)}
        except FileNotFoundError:
            before = None
        except (ValueError, TypeError):
            log.warning("optouts file unreadable, written again from the database")
            before = None
        if before is None or not ids <= before:
            alerts.write_private(path, json.dumps(sorted(ids | (before or set()))) + "\n")
    except Exception as e:  # noqa: BLE001 - sd.write or db.unreachable says why
        log.warning("optouts not saved", extra={"error": type(e).__name__})


async def run_once(settings: Settings, *, now: datetime | None = None, bot: Bot | None = None,
                   http: aiohttp.ClientSession | None = None, systemctl=run_systemctl, host: Host | None = None,
                   state_dir: Path = STATE_DIR, runtime_dir: Path | None = None, checks: tuple[Check, ...] = CHECKS,
                   dry_run: bool = False) -> Report:
    """One run. With dry_run nothing is sent, written or saved: the report says what would have been.
    Busy when another run holds the state. runtime_dir: where the state is also kept in memory (None: not)."""
    now = now or datetime.now(UTC)
    held = None if dry_run else lock(state_dir)
    try:
        return await _run(settings, now, bot, http, systemctl, host or Host(), state_dir, runtime_dir, checks, dry_run)
    finally:
        if held is not None:
            os.close(held)


async def _run(settings: Settings, now: datetime, bot: Bot | None, http: aiohttp.ClientSession | None, systemctl,
               host: Host, state_dir: Path, runtime_dir: Path | None, checks: tuple[Check, ...],
               dry_run: bool) -> Report:
    tz = ZoneInfo(settings.timezone)
    results = [card_result(state_dir)]
    state = load_state(state_dir, runtime_dir)
    conn, db_now, error = await connect(settings.database_url)
    ctx = Ctx(settings, now, tz, state, conn, db_now, http, systemctl, host)
    try:
        if conn is not None:
            try:
                published = await conn.fetchval("SELECT value FROM ops_state WHERE key = 'watchdog'")
                ctx.state = state = alerts.adopt(state, published)
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog snapshot unreadable", extra={"error": type(e).__name__})
        results.append(db_result(ctx, error))
        for check in checks:
            if due(check, state, now):
                results.append(await run_check(ctx, check))
        state, out = alerts.evaluate(state, results, now, tz=tz, quiet=settings.alert_quiet)

        chat, summary, sent = settings.alert_chat_id, None, False
        if not dry_run and bot is not None and chat is not None:
            if alerts.daily_due(state, now, tz, settings.alert_daily_at):
                summary = await _summary(ctx, bot)
            if out is not None and await notify.send(bot, chat, out.text, silent=out.silent):
                alerts.delivered(state, out, now)
                sent = True
            room = len([t for t in state.sent_times if now - t < timedelta(hours=1)]) < alerts.MAX_PER_HOUR
            if summary is not None and room and await notify.send(bot, chat, summary, silent=True):
                state.last_daily = now.astimezone(tz).date()
                state.sent_times.append(now)
                if ctx.summary_restarts is not None:
                    state.baselines["summary_restarts"] = ctx.summary_restarts

        snap = alerts.snapshot(state, now)
        if not dry_run:
            if ctx.conn is not None:
                await _publish(ctx.conn, snap)
                await keep_optouts(ctx.conn, state_dir)
            keep_in_memory(runtime_dir, state, now)
            try:
                alerts.save_state(state_dir / STATE_FILE, state, now)
            except OSError as e:     # sd.write says it; the snapshot and the copy in memory keep what was said
                log.warning("watchdog state not saved", extra={"error": type(e).__name__})
        log.info("watchdog run", extra={"checks": {r.name: r.status for r in results},
                                        "open": [p["key"] for p in snap["problems"]], "sent": sent,
                                        "summary": summary is not None})
        return Report(results, snap, out, summary, sent)
    finally:
        if ctx.conn is not None:
            await ctx.conn.close()


def mute(state_dir: Path, minutes: int, now: datetime | None = None, *,
         runtime_dir: Path | None = None) -> datetime | None:
    """--mute: until now + minutes, new problems are held and messages arrive without sound; 0 ends it."""
    now = now or datetime.now(UTC)
    held = lock(state_dir, wait=True)
    try:
        state = load_state(state_dir, runtime_dir)
        state.mute_until = now + timedelta(minutes=minutes) if minutes > 0 else None
        alerts.save_state(state_dir / STATE_FILE, state, now)      # OSError: the card is where a mute is kept
        keep_in_memory(runtime_dir, state, now)
        return state.mute_until
    finally:
        if held is not None:
            os.close(held)
