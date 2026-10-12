"""Stage 4 watchdog, the checks against the real database and a whole run: what each check reads (the
collector's heartbeat, maintenance_runs, nestor.backup's status.json, systemctl), what it says, and that
it only reads. systemctl, the clock's sync, the card and Telegram are stood in for."""
import asyncio
import json
import os
import threading
from collections import namedtuple
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from nestor import backup as bk
from nestor import retention
from nestor.bot import markup, prompts
from nestor.bot.daily import day_name
from nestor.config import Settings
from nestor.watchdog import alerts, checks, run, summary
from nestor.watchdog.alerts import AlertState
from nestor.watchdog.checks import CHECKS, Check, Ctx, Finding, result
from nestor.watchdog.probes import Host, ProbeError

from .factories import CHAT_ID
from .test_backup import NIGHT, Tools, cfg_in, old_dumps

MSK = ZoneInfo("Europe/Moscow")
ALERT_CHAT = -1001000000555
GIB = 1024 ** 3
Usage = namedtuple("Usage", "total used free")
BY_NAME = {c.name: c for c in CHECKS}

ACTIVE = {"ActiveState": "active", "SubState": "running", "Result": "success", "NRestarts": "0",
          "ExecMainStatus": "0", "MemoryPeak": str(150 * 1024 ** 2)}


class Systemd:
    """`systemctl show UNIT -p ...` for the units it is given; an unknown unit is not found, as systemd says."""

    def __init__(self, **units: dict):
        self.units = {"nestor-collector": dict(ACTIVE), "nestor-backup": {"ActiveState": "inactive"}}
        self.units.update({name.replace("_", "-"): props for name, props in units.items()})
        self.calls: list[tuple] = []

    async def __call__(self, *args: str) -> str:
        self.calls.append(args)
        assert args[0] == "show" and args[2] == "-p"
        props = self.units.get(args[1], {"LoadState": "not-found", "ActiveState": "inactive"})
        return "".join(f"{k}={v}\n" for k, v in props.items())


class FailingSystemd(Systemd):
    """systemctl that fails for these units, as nestor.watchdog.probes.systemctl does (ProbeError)."""

    def __init__(self, *failing: str, **units: dict):
        super().__init__(**units)
        self.failing = set(failing)

    async def __call__(self, *args: str) -> str:
        if args[1] in self.failing:
            raise ProbeError("systemctl_exit_4")
        return await super().__call__(*args)


class AlertBot(Bot):
    """Telegram for the watchdog: what it sends, and how many are in the alert group."""

    def __init__(self, members: int = 2, token: str = "42:TEST"):
        super().__init__(token)
        self.sent: list[dict] = []
        self.members = members
        self.errors: list[Exception] = []
        self.count_errors: list[Exception] = []

    async def send_message(self, chat_id, text, **kw):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return True

    async def get_chat_member_count(self, chat_id, **kw):
        if self.count_errors:
            raise self.count_errors.pop(0)
        return self.members


async def synced() -> bool:
    return True


def host(free_gb: float = 40, *, readable: bool = False, is_synced=synced) -> Host:
    return Host(synced=is_synced, disk_usage=lambda path: Usage(58 * GIB, 0, int(free_gb * GIB)),
                readable=lambda path: readable)


def settings(database_url: str = "", **kw) -> Settings:
    return Settings(bot_token="42:TEST", database_url=database_url, allowed_chat_ids=frozenset({CHAT_ID, ALERT_CHAT}),
                    alert_chat_id=ALERT_CHAT, **kw)


async def make_ctx(conn, *, now=None, state=None, systemd=None, hst=None, **kw) -> Ctx:
    db_now = await conn.fetchval("SELECT clock_timestamp()") if conn is not None else None
    return Ctx(settings(**kw), now or db_now, MSK, state or AlertState(), conn, db_now, None, systemd or Systemd(),
               hst or host())


async def check(ctx: Ctx, name: str):
    return await run.run_check(ctx, BY_NAME[name])


def keys(res) -> list[str]:
    return [f.key for f in res.findings]


# ---- the schema, the collector, its heartbeat ---------------------------------------------------------------

async def test_migrations_pending_unknown_and_crash_text(conn, monkeypatch):
    ctx = await make_ctx(conn)
    assert (await check(ctx, "migrations")).status == "ok"
    files = checks.migration_files()
    monkeypatch.setattr(checks, "migration_files", lambda: [*files, Path("0009_next.sql")])
    res = await check(ctx, "migrations")
    assert keys(res) == ["migrations.pending"] and res.findings[0].confirm == 2
    assert res.findings[0].line == prompts.ALERT_MIGRATIONS.format(versions="0009_next")
    # The collector restarts because the migration fails at its start.
    ctx = await make_ctx(conn, systemd=Systemd(nestor_collector={"ActiveState": "activating", "SubState": "auto-restart",
                                                                  "Result": "exit-code", "NRestarts": "4"}))
    assert (await check(ctx, "migrations")).findings[0].line == prompts.ALERT_MIGRATION_CRASH.format(versions="0009_next")
    # ... or systemd gave up on it.
    ctx = await make_ctx(conn, systemd=Systemd(nestor_collector={"ActiveState": "failed", "SubState": "failed",
                                                                  "Result": "exit-code"}))
    assert (await check(ctx, "migrations")).findings[0].line == prompts.ALERT_MIGRATION_CRASH.format(versions="0009_next")
    # systemctl fails: still pending, only whether it crashes cannot be told.
    res = await check(await make_ctx(conn, systemd=FailingSystemd("nestor-collector")), "migrations")
    assert [(f.key, f.line) for f in res.findings] == [
        ("migrations.pending", prompts.ALERT_MIGRATIONS.format(versions="0009_next"))]
    # The database is newer than the code on the Pi.
    monkeypatch.setattr(checks, "migration_files", lambda: files[:-1])
    res = await check(ctx, "migrations")
    assert keys(res) == ["migrations.unknown"] and files[-1].stem in res.findings[0].line


async def _beat(conn, *, age="0 seconds", **value):
    value = {"started_at": None, "updates_ok_at": None, "updates_failures": 0, "updates_error": None, "sent": 0,
             "send_failed": 0, "bad_request": 0, "flood": 0, **value}
    await conn.execute("""INSERT INTO ops_state (key, value, updated_at) VALUES ('collector', $1, now() - $2::text::interval)
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
                       value, age)


async def test_heartbeat_stale_only_when_unit_active(conn, caplog):
    ctx = await make_ctx(conn)
    assert (await check(ctx, "heartbeat")).status == "unknown"            # a collector without telemetry yet
    assert "heartbeat missing" in caplog.text
    now = ctx.now
    iso = (now - timedelta(seconds=20)).isoformat()
    await _beat(conn, age="4 minutes", started_at=iso, updates_ok_at=iso)
    res = await check(await make_ctx(conn), "heartbeat")
    assert keys(res) == ["collector.hung"] and res.findings[0].confirm == 2 and res.unknown == ("telegram.polling",)
    # Not running at all: that is collector.down's to say.
    stopped = Systemd(nestor_collector={**ACTIVE, "ActiveState": "inactive", "SubState": "dead"})
    assert (await check(await make_ctx(conn, systemd=stopped), "heartbeat")).status == "unknown"
    # Fresh heartbeat, good polls.
    await _beat(conn, started_at=iso, updates_ok_at=iso)
    assert (await check(await make_ctx(conn), "heartbeat")).status == "ok"
    # No good poll for 10 minutes: polling, counted from the last good one.
    ten = (now - timedelta(minutes=10)).isoformat()
    await _beat(conn, started_at=ten, updates_ok_at=ten, updates_failures=40, updates_error="5xx")
    res = await check(await make_ctx(conn), "heartbeat")
    assert keys(res) == ["telegram.polling"] and res.findings[0].confirm == 2
    assert res.findings[0].line == prompts.ALERT_POLLING.format(since=alerts.fmt_since(now - timedelta(minutes=10), now, MSK),
                                                                error="<code>5xx</code>")
    # Not one good poll since it started (the Pi was up before the router's uplink): counted from its start.
    one = (now - timedelta(minutes=1)).isoformat()
    await _beat(conn, started_at=one, updates_ok_at=None, updates_failures=3, updates_error="network")
    assert (await check(await make_ctx(conn), "heartbeat")).status == "ok"
    await _beat(conn, started_at=ten, updates_ok_at=None, updates_failures=40, updates_error="network")
    res = await check(await make_ctx(conn), "heartbeat")
    assert keys(res) == ["telegram.polling"]
    assert res.findings[0].line == prompts.ALERT_POLLING.format(since=alerts.fmt_since(now - timedelta(minutes=10), now, MSK),
                                                                error="<code>network</code>")
    # 401 and 409 are not a passing outage: said on the first run. The watchdog sends with the same token, so
    # the 401 line arrives only once .env has a new one, which the running collector has not read yet.
    assert prompts.ALERT_TOKEN_REVOKED.endswith("<code>sudo systemctl restart nestor-collector</code>")
    for code, text in (("401", prompts.ALERT_TOKEN_REVOKED), ("409", prompts.ALERT_POLLING_CONFLICT)):
        await _beat(conn, started_at=iso, updates_ok_at=iso, updates_failures=1, updates_error=code)
        res = await check(await make_ctx(conn), "heartbeat")
        assert [(f.key, f.line, f.confirm) for f in res.findings] == [("telegram.polling", text, 1)]
    # A code left from a failure that is over says nothing.
    await _beat(conn, started_at=iso, updates_ok_at=iso, updates_failures=0, updates_error="401")
    assert (await check(await make_ctx(conn), "heartbeat")).status == "ok"
    # The Pi's network failing is remembered: the news sources fail with it.
    state = AlertState()
    await _beat(conn, started_at=iso, updates_ok_at=iso, updates_failures=2, updates_error="network")
    assert (await check(await make_ctx(conn, state=state), "heartbeat")).status == "ok"
    assert state.memo["heartbeat"]["network_at"]


async def test_collector_down_and_restarts(conn):
    state = AlertState()
    systemd = Systemd(nestor_collector={**ACTIVE, "NRestarts": "2"})
    assert (await check(await make_ctx(conn, state=state, systemd=systemd), "collector")).status == "ok"
    assert state.baselines["collector_restarts"] == 2                     # the first look only counts
    systemd.units["nestor-collector"] = {**ACTIVE, "NRestarts": "5", "Result": "oom-kill"}
    res = await check(await make_ctx(conn, state=state, systemd=systemd), "collector")
    assert res.status == "ok" and [(e.key, e.count) for e in res.events] == [("collector.restarts", 3)]
    assert res.events[0].line == prompts.ALERT_COLLECTOR_RESTARTS.format(count="{count}",
                                                                         reason="не хватило памяти (oom-kill)")
    # Restarted by hand: NRestarts starts again from 0, which is not a restart of its own.
    systemd.units["nestor-collector"] = {**ACTIVE, "NRestarts": "0"}
    assert (await check(await make_ctx(conn, state=state, systemd=systemd), "collector")).events == []
    systemd.units["nestor-collector"] = {**ACTIVE, "NRestarts": "1", "Result": "success"}
    res = await check(await make_ctx(conn, state=state, systemd=systemd), "collector")
    assert res.events[0].line.endswith(prompts.RESTART_UNKNOWN)
    # Down: crit after two runs, with what systemd says.
    systemd.units["nestor-collector"] = {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code",
                                         "NRestarts": "1"}
    res = await check(await make_ctx(conn, state=state, systemd=systemd), "collector")
    assert [(f.key, f.level, f.confirm) for f in res.findings] == [("collector.down", "crit", 2)]
    assert "<code>failed/failed, exit-code</code>" in res.findings[0].line
    # Back, then down again days later: said from when it went down this time, not the first time.
    systemd.units["nestor-collector"] = {**ACTIVE, "NRestarts": "1"}
    back = await make_ctx(conn, state=state, systemd=systemd, now=datetime.now(MSK) + timedelta(minutes=10))
    assert (await check(back, "collector")).status == "ok" and state.memo["collector"] == {}
    systemd.units["nestor-collector"] = {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code",
                                         "NRestarts": "1"}
    again = await make_ctx(conn, state=state, systemd=systemd, now=back.now + timedelta(days=4))
    assert (await check(again, "collector")).findings[0].line == prompts.ALERT_COLLECTOR_DOWN.format(
        since=alerts.fmt_since(again.now, again.now, MSK), state="<code>failed/failed, exit-code</code>")


async def test_restarts_before_the_first_run_after_a_reboot_are_not_the_collectors(database, conn, tmp_path):
    bot, good = AlertBot(), settings(database["collector"], alert_daily_at=None)
    systemd = Systemd()
    now = datetime.now(MSK).replace(hour=14)

    async def run_on(boot, minutes, restarts, result="success"):
        systemd.units["nestor-collector"] = {**ACTIVE, "NRestarts": str(restarts), "Result": result}
        await run.run_once(good, now=now + timedelta(minutes=minutes), bot=bot, systemctl=systemd,
                           host=replace(host(), boot_id=lambda: boot), state_dir=tmp_path,
                           checks=(BY_NAME["collector"],))

    await run_on("boot-a", 0, 0)
    # A power cut: the Pi is up before the router, and the collector restarts until the database is.
    await run_on("boot-b", 63, 9)
    assert bot.sent == [] and json.loads((tmp_path / run.STATE_FILE).read_text())["baselines"] == {
        "collector_restarts": 9}
    await run_on("boot-b", 68, 11, "oom-kill")             # its own, after that
    assert [m["text"] for m in bot.sent] == ["\n".join([prompts.ALERT_EVENTS, "• " + prompts.ALERT_COLLECTOR_RESTARTS.format(
        count=2, reason="не хватило памяти (oom-kill)")])]


async def test_db_unreachable_clears_after_two_good_runs(conn):
    ctx, state = await make_ctx(conn), AlertState()
    for error in ("ConnectionRefusedError", "ConnectionRefusedError", None):
        alerts.evaluate(state, [checks.db_result(ctx, error)], ctx.now, tz=MSK, quiet=None)
    assert "db.unreachable" in state.problems                              # one good run is not enough
    alerts.evaluate(state, [checks.db_result(ctx, None)], ctx.now, tz=MSK, quiet=None)
    assert "db.unreachable" not in state.problems
    # Down again days later: said from when it went down this time, not the first time.
    later = await make_ctx(conn, now=ctx.now + timedelta(days=4), state=ctx.state)
    assert checks.db_result(later, "ConnectionRefusedError").findings[0].line == prompts.ALERT_DB_DOWN.format(
        since=alerts.fmt_since(later.now, later.now, MSK), error=prompts.DB_ERRORS["ConnectionRefusedError"])


async def test_db_down_makes_db_checks_unknown_and_suppresses_collector(database, pool, tmp_path):
    bot = AlertBot()
    dead = "postgresql://nestor:wrong@127.0.0.1:1/nestor_test"
    crashing = Systemd(nestor_collector={"ActiveState": "activating", "SubState": "auto-restart", "Result": "exit-code",
                                         "NRestarts": "3"})
    now = datetime.now(MSK).replace(hour=14)
    reports = []
    for minutes in (0, 5):
        crashing.units["nestor-collector"]["NRestarts"] = str(3 + minutes)
        reports.append(await run.run_once(settings(dead, alert_daily_at=None), now=now + timedelta(minutes=minutes),
                                          bot=bot, systemctl=crashing, host=host(), state_dir=tmp_path))
    statuses = {r.name: r.status for r in reports[1].results}
    assert statuses["db"] == "problems"
    for name in ("clock", "migrations", "heartbeat", "model", "retention", "reminders", "news", "collector"):
        assert statuses[name] == "unknown", name
    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == "\n".join([prompts.ALERT_NEW, "🔴 " + prompts.ALERT_DB_DOWN.format(
        since=alerts.fmt_since(now, now, MSK), error="в соединении отказано")])
    assert bot.sent[0]["disable_notification"] is False and bot.sent[0]["parse_mode"] == "HTML"
    # Back: the restarts while it was down are not news, and two good runs resolve it.
    good = settings(database["collector"], alert_daily_at=None)
    for minutes in (10, 15):
        crashing.units["nestor-collector"] = {**ACTIVE, "NRestarts": "12"}
        await run.run_once(good, now=now + timedelta(minutes=minutes), bot=bot, systemctl=crashing, host=host(),
                           state_dir=tmp_path, checks=tuple(c for c in CHECKS if c.name != "clock"))
    assert len(bot.sent) == 2 and "снова в порядке" in bot.sent[1]["text"] and "перезапускался" not in bot.sent[1]["text"]


# ---- retention ---------------------------------------------------------------------------------------------

async def test_retention_check_reads_maintenance_runs(conn):
    ctx = await make_ctx(conn)
    assert (await check(ctx, "retention")).status == "ok"                 # 0008 was applied minutes ago
    later = await make_ctx(conn)
    later.db_now += timedelta(hours=27)                                   # never ran since it could
    res = await check(later, "retention")
    assert keys(res) == ["retention.stale"]
    today = ctx.now.astimezone(MSK).date()

    async def last_run(status, *, attempts=1, counts=None, error=None, ago="1 minute", finished="1 minute"):
        await conn.execute("DELETE FROM maintenance_runs")
        await conn.execute(
            """INSERT INTO maintenance_runs (job, day, status, attempts, counts, error, updated_at, finished_at)
               VALUES ('retention', $1, $2, $3, $4, $5, now() - $6::text::interval, now() - $7::text::interval)""",
            today, status, attempts, counts or {}, error, ago, finished)

    await last_run("ok", counts={"messages": 12})
    assert (await check(await make_ctx(conn), "retention")).status == "ok"
    await last_run("ok", ago="27 hours", finished="27 hours")
    assert keys(await check(await make_ctx(conn), "retention")) == ["retention.stale"]
    # A failure is claimed again 10 minutes later: said only when it stays failed.
    await last_run("failed", attempts=2, error="clock_skew")
    assert keys(await check(await make_ctx(conn), "retention")) == []
    for attempts, ago in ((2, "25 minutes"), (6, "1 minute")):
        await last_run("failed", attempts=attempts, error="clock_skew", ago=ago)
        res = await check(await make_ctx(conn), "retention")
        failed = [f for f in res.findings if f.key == "retention.failed"]
        assert failed[0].line == prompts.ALERT_RETENTION_FAILED.format(reason=prompts.RETENTION_REASONS["clock_skew"])
        assert failed[0].repeat_token == str(today)                       # said again on another night
    await last_run("failed", attempts=6, error="SerializationError")       # a code without words of its own
    res = await check(await make_ctx(conn), "retention")
    assert "<code>SerializationError</code>" in res.findings[0].line
    await last_run("failed", attempts=6, error="Bad <Error> from a server")  # not a code: not shown
    res = await check(await make_ctx(conn), "retention")
    assert res.findings[0].line == prompts.ALERT_RETENTION_FAILED.format(
        reason=prompts.RETENTION_ERROR.format(error="error"))
    await last_run("blocked", counts={"would_delete": 6000, "total": 9000}, error="too_many")
    res = await check(await make_ctx(conn), "retention")
    assert [(f.line, f.label) for f in res.findings if f.key == "retention.blocked"] == [
        (prompts.ALERT_RETENTION_BLOCKED.format(count=6000, total=9000), "Большая чистка")]


async def test_a_failed_night_stays_open_while_the_next_one_is_tried_again(conn):
    today, state = (await conn.fetchval("SELECT clock_timestamp()")).astimezone(MSK).date(), AlertState()

    async def night(day, status, attempts):
        await conn.execute(
            """INSERT INTO maintenance_runs (job, day, status, attempts, error, updated_at, finished_at)
               VALUES ('retention', $1, $2, $3, $4, now() - interval '1 minute', now() - interval '1 minute')
               ON CONFLICT (job, day) DO UPDATE SET status = EXCLUDED.status, attempts = EXCLUDED.attempts,
                                                    error = EXCLUDED.error""",
            day, status, attempts, "clock_skew" if status == "failed" else None)
        ctx = await make_ctx(conn, state=state)
        _, out = alerts.evaluate(state, [await check(ctx, "retention")], ctx.now, tz=MSK, quiet=None)
        if out is not None:
            alerts.delivered(state, out, ctx.now)
        return out and out.text

    said = prompts.ALERT_RETENTION_FAILED.format(reason=prompts.RETENTION_REASONS["clock_skew"])
    assert said in await night(today - timedelta(days=1), "failed", 6)
    # The next night fails at first and is tried again: nothing is resolved before its verdict is in.
    assert await night(today, "failed", 1) is None
    assert await night(today, "running", 2) is None and "retention.failed" in state.problems
    assert "снова в порядке" in await night(today, "ok", 2) and state.problems == {}


async def test_retention_overdue_every_kind(pool, conn):
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT_ID)
    for uid in (111, 222):
        await conn.execute("INSERT INTO users (user_id, first_name, updated_at) VALUES ($1, 'U', now() - interval '2 years')",
                           uid)
    await conn.execute("""INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw, stored_at)
                          VALUES ($1, 1, 111, now(), 'старое', '{}', now() - interval '13 months')""", CHAT_ID)
    await conn.execute("""INSERT INTO bot_requests (chat_id, status, created_at, oldest_source_at)
                          VALUES ($1, 'ok', now() - interval '13 months', now() - interval '13 months')""", CHAT_ID)
    await conn.execute("""INSERT INTO bot_requests (chat_id, status, answer, created_at, oldest_source_at)
                          VALUES ($1, 'ok', 'ответ', now() - interval '3 days', now() - interval '13 months')""", CHAT_ID)
    # A follow-up to an old answer inherits its sources: logged just now, it is the next night's to take.
    await conn.execute("""INSERT INTO bot_requests (chat_id, status, answer, oldest_source_at)
                          VALUES ($1, 'ok', 'ответ', now() - interval '13 months')""", CHAT_ID)
    await conn.execute("""INSERT INTO reminders (chat_id, user_id, request_msg_id, text, due_at, created_at, sent_at)
                          VALUES ($1, 111, 1, 'x', now() - interval '13 months', now() - interval '13 months',
                                  now() - interval '13 months')""", CHAT_ID)
    res = await check(await make_ctx(conn), "retention")
    overdue = [f for f in res.findings if f.key == "retention.overdue"]
    assert overdue[0].level == "crit" and overdue[0].line == prompts.ALERT_RETENTION_OVERDUE.format(
        what="сообщения: 1, ответы: 1, запросы: 1, напоминания: 1, имена: 1", months=12)
    # What the nightly run removes is not overdue any more.
    _, cutoff = await retention.db_times(conn)
    await retention.purge(pool, cutoff, pause_s=0)
    assert keys(await check(await make_ctx(conn), "retention")) == []


# ---- backups: the status nestor.backup writes ---------------------------------------------------------------

async def test_backup_contract_from_backup_write_status(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "TIMESYNC_FLAG", tmp_path / "no-timesync-flag")     # Tools(synced=...) decides
    cfg = cfg_in(tmp_path)
    after = NIGHT + timedelta(minutes=30)

    async def look(now=after, systemd=None, readable=False):
        ctx = await make_ctx(conn, now=now, systemd=systemd, hst=host(readable=readable),
                             backup_status_file=str(cfg.status_file))
        return await check(ctx, "backup")

    # BACKUP_STATUS_FILE empty: backups are not watched, and /status and the summary say so.
    off = await make_ctx(conn, now=after)
    res = await check(off, "backup")
    assert res.status == "unknown" and summary._backup(off) == [prompts.DAILY_BACKUP_OFF]
    alerts.evaluate(off.state, [res], after, tz=MSK, quiet=None)
    assert alerts.render_status(alerts.snapshot(off.state, after), after, MSK).endswith(
        prompts.STATUS_UNCHECKED.format(names="Резервные копии"))
    res = await look()
    assert [(f.key, f.level, f.line) for f in res.findings] == [
        ("backup.missing", "crit", prompts.ALERT_BACKUP_MISSING)]
    bk.backup(cfg, NIGHT, run=Tools(size=1000))
    assert (await look()).status == "ok"
    # Stale: no good dump for more than 26 hours.
    res = await look(now=after + timedelta(hours=27))
    assert keys(res) == ["backup.stale"] and "5 октября 04:30" in res.findings[0].line
    # A failed night, then a second one: said each night.
    down = "pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused"
    bk.backup(replace(cfg, attempts=1), NIGHT + timedelta(days=1), run=Tools(down))
    res = await look(now=after + timedelta(days=1))
    assert [(f.key, f.repeat_token) for f in res.findings] == [("backup.failed", "1")]
    assert res.findings[0].line == prompts.ALERT_BACKUP_FAILED.format(
        nights=1, reason="база на роутере недоступна", last_ok="5 октября 04:30")
    bk.backup(replace(cfg, attempts=1), NIGHT + timedelta(days=2), run=Tools(down))
    res = await look(now=after + timedelta(days=2))
    # The last good copy is 2 days old: said in that line, not once more as stale.
    assert [(f.key, f.repeat_token) for f in res.findings] == [("backup.failed", "2")]
    # The runs stopped after it (the timer): stale again, under a name of its own.
    res = await look(now=after + timedelta(days=3, hours=3))
    assert [(f.key, f.label) for f in res.findings] == [("backup.failed", ""), ("backup.stale", "Свежая копия")]
    # A run that died: `running` is left set while the unit is not running any more.
    with pytest.raises(RuntimeError):
        bk.backup(cfg, NIGHT + timedelta(days=3), run=Tools(RuntimeError("bug")))
    res = await look(now=after + timedelta(days=3))
    assert "backup.interrupted" in keys(res)
    assert [f.line for f in res.findings if f.key == "backup.interrupted"] == [
        prompts.ALERT_BACKUP_INTERRUPTED.format(since="04:30")]
    running = Systemd(nestor_backup={"ActiveState": "activating"})
    assert "backup.interrupted" not in keys(await look(now=after + timedelta(days=3), systemd=running))
    # systemctl fails: whether it still runs cannot be told, and the open problem stays as it is.
    state = AlertState()
    alerts.evaluate(state, [res], after + timedelta(days=3), tz=MSK, quiet=None)
    unsure = await look(now=after + timedelta(days=3, minutes=5), systemd=FailingSystemd("nestor-backup"))
    assert "backup.interrupted" not in keys(unsure) and "backup.interrupted" in unsure.unknown
    alerts.evaluate(state, [unsure], after + timedelta(days=3, minutes=5), tz=MSK, quiet=None)
    assert "backup.interrupted" in state.problems
    # Copies older than promised stay while the clock cannot be trusted to rotate them.
    old_dumps(cfg, [NIGHT - timedelta(days=40)])
    unsynced = bk.backup(cfg, NIGHT + timedelta(days=4), run=Tools(size=1000, synced=False), force=True)
    assert unsynced["warnings"] == ["clock_unsynced"]
    res = await look(now=after + timedelta(days=4))
    assert sorted(keys(res)) == ["backup.old_copies", "backup.warning:clock_unsynced"]
    assert prompts.ALERT_BACKUP_OLD.format(days=44, max_days=35) in [f.line for f in res.findings]
    # Shrank: relayed with what the purges removed since the copy it is compared with. At the cliff that is
    # the large purge confirmed by day and the next night's, not only the last one.
    for path in cfg.dumps_dir.iterdir():
        path.unlink()
    bk.backup(cfg, NIGHT + timedelta(days=5), run=Tools(size=1000), force=True)
    small = bk.backup(cfg, NIGHT + timedelta(days=6), run=Tools(size=100), force=True)
    await conn.execute("""INSERT INTO maintenance_runs (job, day, status, counts, finished_at)
                          VALUES ('retention', '2026-10-09', 'ok', '{"messages": 99}', '2026-10-10 03:31+03'),
                                 ('retention', '2026-10-10', 'ok', '{"messages": 6000}', '2026-10-10 12:00+03'),
                                 ('retention', '2026-10-11', 'ok', '{"messages": 150}', '2026-10-11 03:31+03')""")
    res = await look(now=after + timedelta(days=6))
    assert [(f.key, f.line, f.repeat_token) for f in res.findings] == [
        ("backup.warning:shrank", prompts.ALERT_BACKUP_SHRANK.format(
            pct=90, why=prompts.ALERT_BACKUP_SHRANK_WHY.format(count=6150)), small["file"])]


async def test_a_night_without_a_copy_never_says_one_was_made(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "TIMESYNC_FLAG", tmp_path / "no-timesync-flag")     # Tools(synced=...) decides
    cfg = cfg_in(tmp_path)
    down = "pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused"
    state, said = AlertState(), []

    async def look(now):
        nonlocal state
        ctx = await make_ctx(conn, now=now, state=state, backup_status_file=str(cfg.status_file))
        res = await check(ctx, "backup")
        state, out = alerts.evaluate(state, [res], now, tz=MSK, quiet=None)
        if out is not None:
            alerts.delivered(state, out, now)
            said.append(out.text)
        return res

    bk.backup(cfg, NIGHT - timedelta(days=1), run=Tools(size=1000))
    bk.backup(cfg, NIGHT, run=Tools(size=100))
    await look(NIGHT + timedelta(minutes=15))
    assert said == [prompts.ALERT_NEW + "\n🟠 " + prompts.ALERT_BACKUP_SHRANK.format(pct=90, why="")]
    # A power cut: the missed run starts at boot, before the clock is synced and the router's database is up.
    boot = NIGHT + timedelta(days=1)
    failed = bk.backup(replace(cfg, attempts=1), boot, run=Tools(down, synced=False))
    assert (failed["ok"], failed["error"], failed["warnings"]) == (False, "unreachable", ["clock_unsynced"])
    res = await look(boot + timedelta(minutes=15))
    assert [(f.key, f.line, f.label, f.repeat_token) for f in res.findings if f.key.startswith("backup.warning")] == [
        ("backup.warning:clock_unsynced", prompts.ALERT_BACKUP_NOTE.format(warning=prompts.BACKUP_WARNINGS[
            "clock_unsynced"]), "Удаление старых копий", "1")]
    assert "Резервная копия не сделана (1-я ночь)" in said[-1] and "сделана, но" not in said[-1]
    # No new copy: whether one shrank is not news either way.
    assert "backup.warning:shrank" in state.problems and "снова в порядке" not in said[-1]
    # 13:30: the clock is synced, the database still down. The old copies were looked at; the backups are not fine.
    retry = boot.replace(hour=13, minute=30)
    assert bk.backup(replace(cfg, attempts=1), retry, run=Tools(down), force=True)["warnings"] == []
    await look(retry + timedelta(minutes=15))
    assert "• Удаление старых копий: снова в порядке" in said[-1]
    assert "Резервные копии: снова в порядке" not in "\n".join(said) and "сделана, но" not in "\n".join(said)
    assert {"backup.failed", "backup.warning:shrank"} <= set(state.problems)
    assert "Свежей резервной копии нет" not in "\n".join(said)     # the failed nights say it, with the last good one
    # The next copy: the size is news again, and the backups are fine.
    bk.backup(cfg, NIGHT + timedelta(days=2), run=Tools(size=100))
    await look(NIGHT + timedelta(days=2, minutes=15))
    assert "• Размер копии: снова в порядке" in said[-1] and "• Резервные копии: снова в порядке" in said[-1]
    assert state.problems == {}


async def test_a_failed_night_rings_once(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "TIMESYNC_FLAG", tmp_path / "no-timesync-flag")     # Tools(synced=...) decides
    cfg = cfg_in(tmp_path)
    down = "pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused"
    state, said = AlertState(), []

    async def look(now):
        nonlocal state
        ctx = await make_ctx(conn, now=now, state=state, backup_status_file=str(cfg.status_file))
        state, out = alerts.evaluate(state, [await check(ctx, "backup")], now, tz=MSK, quiet=(time(23), time(8)))
        if out is not None:
            alerts.delivered(state, out, now)
            said.append(out)

    bk.backup(cfg, NIGHT - timedelta(days=1), run=Tools(size=1000))
    bk.backup(replace(cfg, attempts=1), NIGHT, run=Tools(down))
    for minutes in range(15, 5 * 60, 15):                  # 04:45 to 09:30
        await look(NIGHT + timedelta(minutes=minutes))
    assert [o.silent for o in said] == [True, False]       # said at once, rung after an hour
    assert "Свежей резервной копии нет" not in "\n".join(o.text for o in said)    # 26 h after the last good one
    bk.backup(cfg, NIGHT.replace(hour=13, minute=30), run=Tools(size=1000), force=True)
    await look(NIGHT.replace(hour=13, minute=45))
    assert said[-1].text.count("снова в порядке") == 1 and "• Резервные копии: снова в порядке" in said[-1].text


async def test_a_backup_that_failed_with_the_database_waits_for_it(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "TIMESYNC_FLAG", tmp_path / "no-timesync-flag")
    cfg = cfg_in(tmp_path)
    down = "pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused"
    bk.backup(cfg, NIGHT - timedelta(days=1), run=Tools(size=1000))
    bk.backup(replace(cfg, attempts=1), NIGHT, run=Tools(down))
    ctx = await make_ctx(conn, now=NIGHT + timedelta(minutes=30), backup_status_file=str(cfg.status_file))
    for _ in range(2):
        alerts.evaluate(ctx.state, [checks.db_result(ctx, "ConnectionRefusedError")], ctx.now, tz=MSK, quiet=None)
    res = await check(ctx, "backup")                      # db.unreachable says it: no second alarm for one cause
    assert keys(res) == [] and "backup.failed" in res.unknown
    ctx.conn = None                                       # the run could not connect at all
    assert "backup.failed" in (await check(ctx, "backup")).unknown
    ctx = await make_ctx(conn, now=NIGHT + timedelta(minutes=30), backup_status_file=str(cfg.status_file))
    assert keys(await check(ctx, "backup")) == ["backup.failed"]     # the database is fine: the backup's own


async def test_backup_old_copies_from_the_promised_age(conn, tmp_path):
    status = tmp_path / "status.json"
    now = await conn.fetchval("SELECT clock_timestamp()")
    for age, expected in ((timedelta(days=34), []), (timedelta(days=35, hours=1), ["backup.old_copies"])):
        status.write_text(json.dumps({"ok": True, "last_ok_at": now.isoformat(),
                                      "oldest_dump_at": (now - age).isoformat()}))
        assert keys(await check(await make_ctx(conn, now=now, backup_status_file=str(status)), "backup")) == expected


async def test_backup_dir_readable_by_nestor_is_alerted(conn, tmp_path):
    cfg = cfg_in(tmp_path)
    bk.backup(cfg, NIGHT, run=Tools())
    seen = []

    def readable(path):
        seen.append(path)
        return True

    ctx = await make_ctx(conn, now=NIGHT + timedelta(hours=1), backup_status_file=str(cfg.status_file),
                         hst=replace(host(), readable=readable))
    res = await check(ctx, "backup")
    assert [(f.key, f.level, f.line) for f in res.findings] == [("backup.exposed", "warn", prompts.ALERT_BACKUP_EXPOSED)]
    assert seen == [cfg.dumps_dir]
    # As the unit leaves it (0700, owned by nestor-backup) the user nestor cannot list it.
    assert cfg.dumps_dir.stat().st_mode & 0o777 == 0o700


# ---- the clock, the model, reminders, news ------------------------------------------------------------------

async def test_clock_skew_and_unsynced(conn):
    base = await make_ctx(conn)
    assert (await check(base, "clock")).status == "ok"
    for ahead, level, text in ((timedelta(minutes=5), "warn", prompts.ALERT_CLOCK_DRIFT),
                               (timedelta(minutes=15), "warn", prompts.ALERT_CLOCK_SKEW),
                               (-timedelta(hours=2), "crit", prompts.ALERT_CLOCK_SKEW)):
        ctx = await make_ctx(conn)
        ctx.now = ctx.db_now + ahead
        res = await check(ctx, "clock")
        assert [(f.key, f.level, f.line, f.clear) for f in res.findings] == [
            ("clock", level, text.format(skew=alerts.fmt_duration(abs(ahead))), 2)]

    async def unsynced() -> bool:
        return False

    state = AlertState()
    ctx = await make_ctx(conn, state=state, hst=host(is_synced=unsynced))
    started = ctx.now
    assert (await check(ctx, "clock")).status == "ok"                      # not for 30 minutes yet
    ctx = await make_ctx(conn, state=state, hst=host(is_synced=unsynced))
    ctx.now = ctx.db_now = ctx.db_now + timedelta(minutes=31)
    res = await check(ctx, "clock")
    assert [f.line for f in res.findings] == [prompts.ALERT_CLOCK_UNSYNCED.format(since=alerts.fmt_since(started, ctx.now, MSK))]
    assert (await check(await make_ctx(conn, state=state), "clock")).status == "ok" and state.memo["clock"] == {}


async def test_model_errors(conn):
    async def requests(status, n, *, scenario="answer", model=None):
        for _ in range(n):
            await conn.execute("INSERT INTO bot_requests (chat_id, scenario, status, model) VALUES ($1, $2, $3, $4)",
                               CHAT_ID, scenario, status, model)

    await requests("error", 3, scenario="export")                         # sending a file, not the model
    await requests("ok", 2, scenario="reminder")                          # no model involved
    ctx = await make_ctx(conn)
    assert (await check(ctx, "model")).status == "ok"
    await requests("error", 2)
    assert (await check(ctx, "model")).status == "ok"                      # 2 of 2: fewer than 3
    await requests("timeout", 1)
    await requests("ok", 3, model="claude-test")
    res = await check(ctx, "model")
    assert [f.line for f in res.findings] == [prompts.ALERT_MODEL_ERRORS.format(failed=3, total=6)]
    await requests("ok", 1, model="claude-test")
    assert (await check(ctx, "model")).status == "ok"                      # under half


async def test_reminders_late_confirm_two(conn):
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup'), (-1001, 'supergroup')", CHAT_ID)
    await conn.execute("UPDATE chats SET enabled = FALSE WHERE chat_id = -1001")

    async def reminder(chat, due, **kw):
        await conn.execute(f"""INSERT INTO reminders (chat_id, user_id, request_msg_id, text, due_at, sent_at, cancelled_at)
                               VALUES ($1, 111, 1, 'x', now() - $2::text::interval, {kw.get('sent', 'NULL')},
                                       {kw.get('cancelled', 'NULL')})""", chat, due)

    await reminder(CHAT_ID, "10 minutes")                                 # due, still within its 15 minutes
    await reminder(CHAT_ID, "2 hours", sent="now()")
    await reminder(CHAT_ID, "2 hours", cancelled="now()")
    await reminder(-1001, "2 hours")                                      # a paused group sends nothing
    assert (await check(await make_ctx(conn), "reminders")).status == "ok"
    await reminder(CHAT_ID, "40 minutes")
    await reminder(CHAT_ID, "20 minutes")
    res = await check(await make_ctx(conn), "reminders")
    assert [(f.key, f.confirm, f.clear) for f in res.findings] == [("reminders.late", 2, 1)]
    assert res.findings[0].line == prompts.ALERT_REMINDERS_LATE.format(count=2, ago="40 мин")


async def test_news_aggregated_key(conn):
    async def source(key, *, failures=0, ok_ago="1 hour", run_ago="10 minutes", status="ok"):
        await conn.execute("""INSERT INTO news_sources (key, last_run_at, last_ok_at, status, failures)
                              VALUES ($1, now() - $2::text::interval, now() - $3::text::interval, $4, $5)""",
                           key, run_ago, ok_ago, status, failures)

    await source("pubmed")
    await source("biorxiv")
    await source("reggaeville", failures=5, ok_ago="3 days", status="http_403")
    await source("arctic", failures=3, ok_ago="2 hours", status="timeout")       # failing, but not for a day
    await source("old", failures=9, ok_ago="9 days", run_ago="5 days")           # not read any more
    ctx = await make_ctx(conn)
    assert (await check(ctx, "news")).status == "ok"                      # 1 of 4: only the daily summary
    await conn.execute("UPDATE news_sources SET failures = 2, last_ok_at = now() - interval '30 hours', "
                       "status = 'error: Server says <CANARY>' WHERE key = 'arctic'")
    assert (await check(ctx, "news")).status == "ok"                      # failed twice in a row: not yet
    await conn.execute("UPDATE news_sources SET failures = 3 WHERE key = 'arctic'")
    res = await check(ctx, "news")
    assert [(f.key, f.line) for f in res.findings] == [
        ("news.sources", prompts.ALERT_NEWS.format(count=2, total=4, names="arctic, reggaeville"))]
    # Right after the Pi's own network failed, they all fail with it: nothing to say about them.
    ctx.state.memo["heartbeat"] = {"network_at": (ctx.now - timedelta(minutes=30)).isoformat()}
    assert (await check(ctx, "news")).status == "unknown"


# ---- read only, opt-outs, a whole run ----------------------------------------------------------------------

async def test_every_check_runs_read_only(conn, monkeypatch, tmp_path):
    seen = {}

    def spy(c: Check) -> Check:
        async def fn(ctx):
            seen[c.name] = await ctx.conn.fetchval("SHOW transaction_read_only")
            return await c.fn(ctx)
        return replace(c, fn=fn)

    ctx = await make_ctx(conn, backup_status_file=str(tmp_path / "status.json"))
    for c in CHECKS:
        await run.run_check(ctx, spy(c))
    assert seen == {c.name: "on" for c in CHECKS}

    async def rogue(ctx):
        await ctx.conn.execute("INSERT INTO ops_state (key) VALUES ('rogue')")
        return result("rogue")

    res = await run.run_check(ctx, Check("rogue", rogue))
    assert (res.status, keys(res)) == ("error", ["watchdog.check:rogue"])
    assert res.findings[0].line == prompts.ALERT_CHECK_BROKEN.format(name="rogue", error="ReadOnlySQLTransactionError")
    assert await conn.fetchval("SELECT count(*) FROM ops_state") == 0
    assert (await check(ctx, "model")).status == "ok"                      # the connection is fine after it


async def test_a_check_that_hangs_loses_the_database_for_the_rest_of_the_run(database, pool, monkeypatch):
    monkeypatch.setattr(run, "CHECK_TIMEOUT_S", 0.2)

    async def hangs(ctx):
        await ctx.conn.execute("SELECT pg_sleep(5)")
        return result("hangs")

    own = await asyncpg.connect(database["collector"])          # its own: the run gives it up
    try:
        ctx = await make_ctx(own)
        res = await run.run_check(ctx, Check("hangs", hangs))
        assert (res.status, res.findings[0].key, res.findings[0].confirm) == ("error", "watchdog.check:hangs", 2)
        assert ctx.conn is None and own.is_closed()
        assert (await check(ctx, "model")).status == "unknown"
    finally:
        own.terminate()


async def test_a_check_that_loses_the_database_is_unknown_not_broken(database):
    async def sleeps(ctx):
        await ctx.conn.execute("SELECT pg_sleep(5)")
        return result("sleeps")

    own = await asyncpg.connect(database["collector"])          # its own: the run gives it up
    killer = await asyncpg.connect(database["collector"])

    async def restart():                                        # PostgreSQL goes away under the check
        await asyncio.sleep(0.3)
        await killer.execute("SELECT pg_terminate_backend($1)", own.get_server_pid())

    try:
        ctx = await make_ctx(own)
        restarting = asyncio.create_task(restart())
        res = await run.run_check(ctx, Check("sleeps", sleeps))
        await restarting
        # db.unreachable speaks for it, not «Проверка sleeps сломалась»; unknown keeps its other problems.
        assert (res.status, res.findings) == ("unknown", [])
        assert ctx.conn is None and own.is_closed()
    finally:
        own.terminate()
        await killer.close()


async def test_optouts_file_accumulates(conn, tmp_path):
    # An anonymous admin's opt-out (from before /forget_me refused it) is no person's: not kept for a restore.
    for uid, out in ((111, True), (222, False), (333, True), (1087968824, True)):
        await conn.execute("INSERT INTO users (user_id, opted_out) VALUES ($1, $2)", uid, out)
    await run.keep_optouts(conn, tmp_path)
    path = tmp_path / run.OPTOUTS_FILE
    assert json.loads(path.read_text()) == [111, 333] and path.stat().st_mode & 0o777 == 0o600
    # A database restored from an older copy forgot 333's opt-out: the file does not.
    await conn.execute("UPDATE users SET opted_out = (user_id = 222)")
    await run.keep_optouts(conn, tmp_path)
    assert json.loads(path.read_text()) == [111, 222, 333]
    assert bk._load_optouts(path) == [111, 222, 333]                    # what restore --optouts reads


async def test_a_run_sends_publishes_the_snapshot_and_the_daily_summary(database, conn, tmp_path):
    await conn.execute("INSERT INTO chats (chat_id, type, title) VALUES ($1, 'supergroup', 'Nestor <test>')", CHAT_ID)
    await conn.execute("INSERT INTO users (user_id, first_name) VALUES (111, 'U')")
    await conn.execute("""INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw, stored_at)
                          SELECT $1, g, 111, now(), 'текст', '{}', date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')
                                 AT TIME ZONE 'Europe/Moscow' - interval '12 hours'
                          FROM generate_series(1, 7) g""", CHAT_ID)
    await conn.execute("INSERT INTO users (user_id, opted_out) VALUES (999, TRUE)")
    db_now = await conn.fetchval("SELECT clock_timestamp()")
    morning = db_now.astimezone(MSK).replace(hour=9, minute=31)
    bot = AlertBot()
    cfg = settings(database["collector"], backup_status_file=str(tmp_path / "missing" / "status.json"))
    no_clock = tuple(c for c in CHECKS if c.name != "clock")
    report = await run.run_once(cfg, now=morning, bot=bot, systemctl=Systemd(), host=host(free_gb=3),
                                state_dir=tmp_path, checks=no_clock)
    alert, summary = bot.sent
    assert alert["text"].startswith(prompts.ALERT_NEW) and not alert["disable_notification"]
    assert "🔴 " + prompts.ALERT_BACKUP_MISSING in alert["text"] and "🟠 " + prompts.ALERT_DISK.format(
        free="3,0 ГБ", total="58,0 ГБ") in alert["text"]
    assert summary["disable_notification"] is True and summary["link_preview_options"].is_disabled
    lines = summary["text"].split("\n")
    yesterday = morning.date() - timedelta(days=1)
    assert lines[0] == prompts.DAILY_HEADER.format(date=day_name(yesterday))
    assert lines[1].startswith("🗄 Архив: «Nestor &lt;test&gt;» 7; база ")
    assert lines[2] == prompts.DAILY_ANSWERS.format(answers=0, errors=0, tokens=0, limit=250_000, pct=0)
    assert lines[3] == prompts.DAILY_PURGE.format(count=0, next=0)
    assert prompts.DAILY_DISK.format(free="3,0 ГБ", total="58,0 ГБ") in lines
    assert prompts.DAILY_COLLECTOR.format(memory="150 МБ", restarts=0) in lines
    assert prompts.DAILY_PROBLEMS.format(count=2) in lines
    assert report.summary == summary["text"] and report.sent
    # The snapshot for /status, the opt-outs, the state.
    snap = await conn.fetchval("SELECT value FROM ops_state WHERE key = 'watchdog'")
    assert snap == report.snapshot and [p["key"] for p in snap["problems"]] == ["backup.missing", "disk.low:/"]
    assert json.loads((tmp_path / run.OPTOUTS_FILE).read_text()) == [999]
    state = alerts.load_state(tmp_path / run.STATE_FILE)
    assert state.last_daily == morning.date() and len(state.sent_times) == 2
    # Later that day: no second summary, nothing new to say; the 15- and 60-minute checks wait their turn.
    report = await run.run_once(cfg, now=morning + timedelta(minutes=5), bot=bot, systemctl=Systemd(),
                                host=host(free_gb=3), state_dir=tmp_path, checks=no_clock)
    assert len(bot.sent) == 2 and report.summary is None
    assert {r.name for r in report.results} == {"sd", "db", "migrations", "collector", "heartbeat", "hermes",
                                                "reminders"}
    # More members than the owner and the bot: only that the summary was not sent.
    tomorrow = morning + timedelta(days=1)
    bot.members = 3
    await run.run_once(cfg, now=tomorrow, bot=bot, systemctl=Systemd(), host=host(), state_dir=tmp_path,
                       checks=no_clock)
    assert bot.sent[-1]["text"] == prompts.ALERT_STRANGERS.format(count=3) and bot.sent[-1]["disable_notification"]
    assert "Место на карте Pi: снова в порядке" in bot.sent[-2]["text"]
    # Telegram down: nothing is lost, the line waits for the next run.
    bot.errors = [TelegramNetworkError(method=None, message="down")]
    sent = len(bot.sent)
    await run.run_once(cfg, now=tomorrow + timedelta(minutes=16), bot=bot, systemctl=Systemd(), host=host(free_gb=3),
                       state_dir=tmp_path, checks=no_clock)
    assert len(bot.sent) == sent and alerts.load_state(tmp_path / run.STATE_FILE).outbox
    await run.run_once(cfg, now=tomorrow + timedelta(minutes=21), bot=bot, systemctl=Systemd(), host=host(free_gb=3),
                       state_dir=tmp_path, checks=no_clock)
    assert bot.sent[-1]["text"] == prompts.ALERT_NEW + "\n🟠 " + prompts.ALERT_DISK.format(free="3,0 ГБ", total="58,0 ГБ")
    assert alerts.load_state(tmp_path / run.STATE_FILE).outbox == []


MIDNIGHT = "(date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow')"     # today's


async def test_daily_summary_reads_backup_news_memory_and_restarts(conn, tmp_path):
    await conn.execute("INSERT INTO chats (chat_id, type, title) VALUES ($1, 'supergroup', 'Nestor')", CHAT_ID)
    await conn.execute("INSERT INTO users (user_id, first_name) VALUES (111, 'U')")
    await conn.execute(f"""INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw, stored_at)
                           VALUES ($1, 1, 111, now(), 'вчера', '{{}}', {MIDNIGHT} - interval '12 hours'),
                                  ($1, 2, 111, now(), 'сегодня', '{{}}', now()),
                                  ($1, 3, 111, now(), 'позавчера', '{{}}', {MIDNIGHT} - interval '36 hours'),
                                  ($1, 4, 111, now(), 'почти год', '{{}}',
                                   now() - interval '12 months' + interval '3 days')""", CHAT_ID)
    # Yesterday's answers and their tokens; not today's, not the day before's.
    await conn.execute(f"""INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, status, input_tokens,
                                                     output_tokens, created_at)
                           VALUES ($1, 1, 111, 'ok', 1000, 500, {MIDNIGHT} - interval '12 hours'),
                                  ($1, 1, 111, 'error', NULL, NULL, {MIDNIGHT} - interval '11 hours'),
                                  ($1, 2, 111, 'ok', 100000, 0, now()),
                                  ($1, 3, 111, 'ok', 200000, 0, {MIDNIGHT} - interval '36 hours')""", CHAT_ID)
    await conn.execute(f"""INSERT INTO maintenance_runs (job, day, status, counts, finished_at)
                           VALUES ('retention', ({MIDNIGHT} - interval '12 hours')::date, 'ok', '{{"messages": 42}}',
                                   now())""")
    await conn.execute("""INSERT INTO news_sources (key, last_run_at, last_ok_at, status, failures)
                          VALUES ('pubmed', now(), now() - interval '1 hour', 'ok', 0),
                                 ('reggaeville', now(), now() - interval '3 days', 'http_403', 5)""")
    cfg = cfg_in(tmp_path)
    bk.backup(cfg, NIGHT - timedelta(days=1), run=Tools())
    good = bk.backup(cfg, NIGHT, run=Tools(size=5 * 1024 ** 2))
    db_now = await conn.fetchval("SELECT clock_timestamp()")
    collector = {**ACTIVE, "MemoryPeak": str(300 * 1024 ** 2), "NRestarts": "5"}
    ctx = await make_ctx(conn, now=db_now.astimezone(MSK).replace(hour=9, minute=30), systemd=Systemd(
        nestor_collector=collector), backup_status_file=str(cfg.status_file))
    ctx.state.baselines["summary_restarts"] = 3                         # what yesterday's summary saw
    lines = (await summary.daily_summary(conn, ctx, AlertBot())).split("\n")
    assert lines[1].startswith(prompts.DAILY_ARCHIVE.format(chats="«Nestor» 1", size=""))  # yesterday's only
    assert lines[2] == prompts.DAILY_ANSWERS.format(answers=1, errors=1, tokens=1500, limit=250_000, pct=1)
    assert lines[3] == prompts.DAILY_PURGE.format(count=42, next=1)     # the one that expires within 7 days
    oldest = datetime.fromisoformat(good["oldest_dump_at"]).astimezone(MSK).date()
    assert prompts.DAILY_BACKUP.format(time=ctx.fmt(datetime.fromisoformat(good["last_ok_at"])), size="5 МБ", kept=2,
                                       oldest=day_name(oldest)) in lines and oldest == date(2026, 10, 4)
    assert prompts.DAILY_COLLECTOR.format(memory="300 МБ" + prompts.DAILY_MEMORY_HIGH, restarts=2) in lines
    assert prompts.DAILY_NEWS.format(names="reggaeville (http_403)") in lines and ctx.summary_restarts == 5
    # Started by hand since: NRestarts counts from 0 again.
    collector.update(NRestarts="1", MemoryPeak=str(200 * 1024 ** 2))
    ctx = await make_ctx(conn, now=ctx.now, systemd=Systemd(nestor_collector=collector), state=ctx.state)
    assert prompts.DAILY_COLLECTOR.format(memory="200 МБ", restarts=1) in (
        await summary.daily_summary(conn, ctx, AlertBot())).split("\n")


async def test_daily_summary_waits_while_the_hours_messages_are_used(database, conn, tmp_path):
    morning = (await conn.fetchval("SELECT clock_timestamp()")).astimezone(MSK).replace(hour=9, minute=31)
    used = AlertState(sent_times=[morning - timedelta(minutes=50 - i) for i in range(alerts.MAX_PER_HOUR)])
    alerts.save_state(tmp_path / run.STATE_FILE, used, morning - timedelta(minutes=5))
    bot, cfg = AlertBot(), settings(database["collector"], alert_quiet=None)
    systemd = Systemd(nestor_collector={**ACTIVE, "NRestarts": "4"})
    no_clock = tuple(c for c in CHECKS if c.name != "clock")
    report = await run.run_once(cfg, now=morning, bot=bot, systemctl=systemd, host=host(), state_dir=tmp_path,
                                checks=no_clock)
    assert report.summary is not None and bot.sent == []
    held = alerts.load_state(tmp_path / run.STATE_FILE)
    assert held.last_daily is None and "summary_restarts" not in held.baselines     # counted from when it goes
    # The first of those six is an hour old: the summary goes, once.
    for minutes in (11, 16):
        await run.run_once(cfg, now=morning + timedelta(minutes=minutes), bot=bot, systemctl=systemd, host=host(),
                           state_dir=tmp_path, checks=no_clock)
    header = prompts.DAILY_HEADER.format(date=day_name(morning.date() - timedelta(days=1)))
    assert [(m["text"].split("\n")[0], m["disable_notification"]) for m in bot.sent] == [(header, True)]
    assert prompts.DAILY_COLLECTOR.format(memory="150 МБ", restarts=4) in bot.sent[0]["text"].split("\n")
    sent = alerts.load_state(tmp_path / run.STATE_FILE)
    assert sent.last_daily == morning.date() and sent.baselines["summary_restarts"] == 4
    # The next morning: the restarts of the day in between.
    systemd.units["nestor-collector"]["NRestarts"] = "6"
    await run.run_once(cfg, now=morning + timedelta(days=1), bot=bot, systemctl=systemd, host=host(),
                       state_dir=tmp_path, checks=no_clock)
    (summary_text,) = [m["text"] for m in bot.sent[1:] if m["text"].startswith(prompts.DAILY_HEADER.format(
        date=day_name(morning.date())))]
    assert prompts.DAILY_COLLECTOR.format(memory="150 МБ", restarts=2) in summary_text.split("\n")
    assert alerts.load_state(tmp_path / run.STATE_FILE).baselines["summary_restarts"] == 6


async def test_no_summary_while_the_members_of_the_alert_group_cannot_be_counted(database, conn, tmp_path):
    morning = (await conn.fetchval("SELECT clock_timestamp()")).astimezone(MSK).replace(hour=9, minute=31)
    bot, cfg = AlertBot(members=3), settings(database["collector"], alert_quiet=None)
    bot.count_errors = [TelegramNetworkError(method=None, message="down")]
    no_clock = tuple(c for c in CHECKS if c.name != "clock")

    async def look(minutes):
        return await run.run_once(cfg, now=morning + timedelta(minutes=minutes), bot=bot, systemctl=Systemd(),
                                  host=host(), state_dir=tmp_path, checks=no_clock)

    # A stranger may have joined: what cannot be counted is not taken for few. Tried again on the next run.
    report = await look(0)
    assert report.summary is None and bot.sent == [] and alerts.load_state(tmp_path / run.STATE_FILE).last_daily is None
    report = await look(5)
    assert [m["text"] for m in bot.sent] == [prompts.ALERT_STRANGERS.format(count=3)]
    assert alerts.load_state(tmp_path / run.STATE_FILE).last_daily == morning.date()


async def test_a_run_and_a_mute_take_turns_on_the_state(tmp_path):
    bot, noon = AlertBot(), datetime(2026, 10, 5, 12, 0, tzinfo=MSK)
    held = run.lock(tmp_path)                                           # a run in progress
    try:
        # Another run says nothing and saves nothing: the one in progress does.
        with pytest.raises(run.Busy):
            await run.run_once(settings("postgresql://nestor:x@127.0.0.1:1/nestor"), now=noon, bot=bot,
                               systemctl=Systemd(), host=host(), state_dir=tmp_path, checks=())
        assert bot.sent == [] and not (tmp_path / run.STATE_FILE).exists()
        # --mute waits for it: the run saves the state it loaded before, then the mute goes on top.
        muting = threading.Thread(target=run.mute, args=(tmp_path, 60, noon))
        muting.start()
        muting.join(0.3)
        assert muting.is_alive()
        alerts.save_state(tmp_path / run.STATE_FILE, AlertState(), noon)
    finally:
        os.close(held)
    muting.join(5)
    assert alerts.load_state(tmp_path / run.STATE_FILE).mute_until == noon + timedelta(minutes=60)


async def test_a_message_telegram_refuses_as_html_goes_as_plain_text(tmp_path):
    bot = AlertBot()
    refused = TelegramBadRequest(method=None, message="Bad Request: can't parse entities")
    bot.errors = [refused]
    cfg = settings("postgresql://nestor:wrong@127.0.0.1:1/nestor_test", alert_daily_at=None, alert_quiet=None)
    now = datetime(2026, 10, 5, 14, 0, tzinfo=MSK)
    report = await run.run_once(cfg, now=now, bot=bot, systemctl=Systemd(), host=host(free_gb=3), state_dir=tmp_path)
    assert report.sent and [(m["text"], m["parse_mode"], m["disable_notification"]) for m in bot.sent] == [
        (markup.to_plain(report.message.text), None, False)]
    assert "<b>" in report.message.text and "<b>" not in bot.sent[0]["text"]
    assert alerts.load_state(tmp_path / run.STATE_FILE).outbox == []
    # Refused as plain text too: nothing is lost, the lines wait for the next run.
    bot.errors = [refused, refused]
    report = await run.run_once(cfg, now=now + timedelta(minutes=15), bot=bot, systemctl=Systemd(),
                                host=host(free_gb=0.5), state_dir=tmp_path)
    assert not report.sent and len(bot.sent) == 1
    assert {i["key"] for i in alerts.load_state(tmp_path / run.STATE_FILE).outbox} == {"db.unreachable", "disk.low:/"}


async def test_a_card_that_cannot_keep_the_state_does_not_repeat_itself(database, conn, tmp_path):
    cfg = cfg_in(tmp_path / "backup")
    down = "pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused"
    bk.backup(replace(cfg, attempts=1), NIGHT, run=Tools(down))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    nine = (await conn.fetchval("SELECT clock_timestamp()")).astimezone(MSK).replace(hour=9, minute=0)
    bot, systemd = AlertBot(), Systemd()
    s = settings(database["collector"], alert_quiet=None, backup_status_file=str(cfg.status_file))
    no_clock = tuple(c for c in CHECKS if c.name != "clock")

    async def once(minutes, free_gb):
        await run.run_once(s, now=nine + timedelta(minutes=minutes), bot=bot, systemctl=systemd,
                           host=host(free_gb=free_gb), state_dir=state_dir, checks=no_clock)

    await once(0, 3)                                    # the backup and the disk, said; the state saved
    assert len(bot.sent) == 1
    # The card can be read but not written: the probe and the state's temporary file cannot be created.
    for name in ("probe", run.STATE_FILE + ".tmp"):
        (state_dir / name).unlink(missing_ok=True)
        (state_dir / name).mkdir()
    systemd.units["nestor-collector"]["NRestarts"] = "3"
    # Past the hour an event waits before it is said again: the checks too work on the state taken from the
    # snapshot, not on the card's stale one. The summary is due from 09:30; the disk is fine again.
    for minutes in range(35, 140, 5):
        await once(minutes, 40)
    assert (state_dir / run.STATE_FILE).exists() and alerts.load_state(state_dir / run.STATE_FILE).saved_at == nine
    header = prompts.DAILY_HEADER.format(date=day_name(nine.date() - timedelta(days=1)))
    summaries = [m["text"] for m in bot.sent if m["text"].startswith(header)]
    alerts_sent = [m["text"] for m in bot.sent if not m["text"].startswith(header)]
    assert len(summaries) == 1 and len(alerts_sent) == 2
    said = "\n".join(alerts_sent)
    for line in ("Резервная копия не сделана", prompts.ALERT_SD_WRITE.format(error="<code>EISDIR</code>"),
                 "перезапускался 3 раз", "перезапускался", "Место на карте Pi: снова в порядке"):
        assert said.count(line) == 1, line
    published = await conn.fetchval("SELECT value FROM ops_state WHERE key = 'watchdog'")
    assert published["state"]["baselines"]["collector_restarts"] == 3


async def test_a_card_that_cannot_keep_the_state_while_the_database_is_down(tmp_path):
    # Then the snapshot cannot help either: the copy of the state in memory (the unit's RuntimeDirectory) does.
    state_dir, runtime_dir = tmp_path / "state", tmp_path / "run"
    state_dir.mkdir()
    runtime_dir.mkdir()
    before = datetime(2026, 10, 5, 13, 55, tzinfo=MSK)
    alerts.save_state(state_dir / run.STATE_FILE, AlertState(), before)
    for name in ("probe", run.STATE_FILE + ".tmp"):
        (state_dir / name).mkdir()
    bot = AlertBot()
    s = settings("postgresql://nestor:x@127.0.0.1:1/nestor", alert_quiet=None, alert_daily_at=time(9, 0))
    sent = []
    for minutes in range(5, 65, 5):                     # an hour of runs
        await run.run_once(s, now=before + timedelta(minutes=minutes), bot=bot, systemctl=Systemd(), host=host(),
                           state_dir=state_dir, runtime_dir=runtime_dir)
        sent.append(len(bot.sent))
    texts = [m["text"] for m in bot.sent]
    header = prompts.DAILY_HEADER.format(date=day_name(date(2026, 10, 4)))
    assert sent[:2] == [2, 3] and len(texts) == 3 <= alerts.MAX_PER_HOUR
    assert [t.startswith(header) for t in texts] == [False, True, False]
    assert texts[0] == prompts.ALERT_NEW + "\n🔴 " + prompts.ALERT_SD_WRITE.format(error="<code>EISDIR</code>")
    assert texts[2] == prompts.ALERT_NEW + "\n🔴 " + prompts.ALERT_DB_DOWN.format(
        since="14:00", error=prompts.DB_ERRORS["ConnectionRefusedError"])          # confirmed on the second run
    assert alerts.load_state(state_dir / run.STATE_FILE).saved_at == before
    kept = alerts.load_state(runtime_dir / run.STATE_FILE)
    assert kept.saved_at == before + timedelta(hours=1) and kept.last_daily == date(2026, 10, 5)
    assert (runtime_dir / run.STATE_FILE).stat().st_mode & 0o777 == 0o600
    # The Pi restarts: the copy in memory is gone, the card's state is what there is.
    (runtime_dir / run.STATE_FILE).unlink()
    assert run.load_state(state_dir, runtime_dir).saved_at == before


async def test_daily_summary_without_the_database_stays_within_a_message():
    ctx = await make_ctx(None, now=datetime(2026, 10, 5, 9, 30, tzinfo=MSK))
    many = result("backup", [Finding(f"backup.warning:w{i:02}", "warn", f"{i:02} " + "длинная строка " * 40)
                             for i in range(12)])
    alerts.evaluate(ctx.state, [many], ctx.now, tz=MSK, quiet=None)
    text = await summary.daily_summary(None, ctx, AlertBot())
    lines = text.split("\n")
    assert lines[:2] == [prompts.DAILY_HEADER.format(date=day_name(date(2026, 10, 4))), prompts.DAILY_DB_DOWN]
    assert prompts.DAILY_PROBLEMS.format(count=12) in lines and len(text) < 4096
    assert lines[-1] == prompts.ALERT_MORE.format(count=12 - sum(line.startswith("🟠 ") for line in lines))
    assert await summary.daily_summary(None, ctx, AlertBot(members=5)) == prompts.ALERT_STRANGERS.format(count=5)


async def test_dry_run_sends_and_saves_nothing(database, conn, tmp_path):
    bot = AlertBot()
    cfg = settings(database["collector"], alert_daily_at=time(0, 0))
    report = await run.run_once(cfg, bot=bot, systemctl=Systemd(), host=host(free_gb=0.5), state_dir=tmp_path,
                                dry_run=True)
    assert bot.sent == [] and report.message is not None and "🔴 " in report.message.text
    assert report.summary is None
    assert not (tmp_path / run.STATE_FILE).exists() and not (tmp_path / run.OPTOUTS_FILE).exists()
    assert await conn.fetchval("SELECT count(*) FROM ops_state") == 0
    assert alerts.render_status(report.snapshot, datetime.now(MSK), MSK).count("🔴") == 1
