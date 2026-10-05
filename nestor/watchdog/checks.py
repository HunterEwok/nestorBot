"""The checks of nestor.watchdog. Each looks at one part of the service and says what is wrong now
(findings), what happened since the last run (events), or that it could not look (unknown).

They only read: the run gives each one a read-only transaction. Their lines are prompts.ALERT_*;
what comes from outside (codes, names) is escaped, and no text a server, a model or a person wrote
ever goes into one.

What they read, besides the database's own tables:
- ops_state 'collector', the collector's heartbeat (nestor.collector.telemetry): updated_at is when
  it last wrote; value has started_at and updates_ok_at (ISO-8601 UTC, the Pi's clock), updates_failures
  (failed getUpdates calls since the last good one), updates_error (the code of the last failure: "401",
  "409", "network", "5xx" or a class name; null when none), and the send counters.
- nestor.backup's status.json (BACKUP_STATUS_FILE), with the keys of nestor.backup.STATUS_KEYS.
- maintenance_runs, job 'retention' (nestor.retention).
- systemctl show nestor-collector and nestor-backup.
"""
from __future__ import annotations

import errno
import html
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg

from .. import retention
from ..bot import prompts
from ..config import Settings
from ..migrate import migration_files
from ..promises import BACKUP_MAX_AGE_DAYS, RETENTION_MONTHS
from .alerts import AlertState, fmt_duration, fmt_since, fmt_size
from .probes import (
    UNIT_PROPERTIES,
    Host,
    ProbeError,
    as_int,
    hermes_profiles,
    parse_show,
    probe_hermes,
    read_json,
    run_systemctl,
    safe_code,
)

log = logging.getLogger("nestor.watchdog")

COLLECTOR = "nestor-collector"
BACKUP = "nestor-backup"
RETENTION_MIGRATION = "0008_retention_and_ops"     # the retention exists since this migration
GIB = 1024 ** 3
HUNG_AFTER = timedelta(minutes=3)       # the heartbeat is written every minute
POLL_STALE = timedelta(minutes=5)
UNSYNCED_FOR = timedelta(minutes=30)
BACKUP_STALE = timedelta(hours=26)
RETENTION_STALE = timedelta(hours=26)
NEWS_FAILING_FOR = timedelta(hours=24)
NEWS_AFTER_NETWORK = timedelta(hours=2)     # sources fail with the Pi's network: not their own fault


@dataclass(frozen=True)
class Finding:
    key: str
    level: str                      # warn | crit
    line: str                       # Telegram HTML
    label: str = ""                 # what «снова в порядке» names; empty: by the key
    confirm: int = 1                # failing runs in a row before it is announced
    clear: int = 1                  # good runs in a row before it is resolved
    repeat_token: str | None = None  # announced again when this changes


@dataclass(frozen=True)
class Event:
    key: str
    line: str                       # "{count}" in it becomes how many happened
    count: int = 1
    cooldown: timedelta = timedelta(hours=1)


@dataclass
class CheckResult:
    name: str
    status: str                     # ok | problems | unknown (could not look) | error (the check broke)
    findings: list[Finding] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    unknown: tuple[str, ...] = ()   # key prefixes it could not look at: neither raised nor cleared


def result(name: str, findings=(), events=(), unknown=()) -> CheckResult:
    return CheckResult(name, "problems" if findings else "ok", list(findings), list(events), tuple(unknown))


def unknown(name: str) -> CheckResult:
    return CheckResult(name, "unknown")


def broken(name: str, error: str, *, confirm: int = 1) -> CheckResult:
    line = prompts.ALERT_CHECK_BROKEN.format(name=html.escape(name), error=html.escape(error))
    return CheckResult(name, "error", [Finding(f"watchdog.check:{name}", "warn", line, confirm=confirm)])


def _code(value) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


@dataclass
class Ctx:
    """What a run knows: the settings, the Pi's time, the alert state (its memo keeps facts between runs),
    the database connection (None while it is unreachable) and its time, the probes."""
    settings: Settings
    now: datetime
    tz: ZoneInfo
    state: AlertState
    conn: asyncpg.Connection | None = None
    db_now: datetime | None = None
    http: aiohttp.ClientSession | None = None
    systemctl: Callable[..., Awaitable[str]] = run_systemctl
    host: Host = field(default_factory=Host)
    summary_restarts: int | None = None     # NRestarts as the daily summary saw it: the next one counts from it
    _units: dict[str, dict] = field(default_factory=dict)
    _rows: dict[str, asyncpg.Record | None] = field(default_factory=dict)

    def fmt(self, at: datetime) -> str:
        return fmt_since(at, self.now, self.tz)

    def memo(self, name: str) -> dict:
        return self.state.memo.setdefault(name, {})

    def since(self, name: str, what: str) -> datetime:
        """When `what` began, as this check first saw it."""
        return datetime.fromisoformat(self.memo(name).setdefault(what, self.now.isoformat()))

    def is_open(self, key: str) -> bool:
        return key in self.state.problems

    async def unit(self, name: str) -> dict[str, str]:
        if name not in self._units:
            self._units[name] = parse_show(await self.systemctl("show", name, "-p", ",".join(UNIT_PROPERTIES)))
        return self._units[name]

    async def heartbeat(self) -> asyncpg.Record | None:
        if "collector" not in self._rows:
            self._rows["collector"] = await self.conn.fetchrow(
                "SELECT value, updated_at FROM ops_state WHERE key = 'collector'")
        return self._rows["collector"]


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[Ctx], Awaitable[CheckResult]]
    every: timedelta = timedelta(minutes=5)
    needs_db: bool = False          # unknown while the database is unreachable


# ---- the database, the clock, the schema ----

def db_result(ctx: Ctx, error: str | None) -> CheckResult:
    """db.unreachable, from the run's own connection attempt (the error's class name)."""
    if error is None:
        ctx.memo("db").clear()
        return result("db")
    line = prompts.ALERT_DB_DOWN.format(since=ctx.fmt(ctx.since("db", "down_since")),
                                        error=prompts.DB_ERRORS.get(error) or _code(error))
    return result("db", [Finding("db.unreachable", "crit", line, confirm=2, clear=2)])


async def check_clock(ctx: Ctx) -> CheckResult:
    skew = abs(ctx.now - ctx.db_now)
    findings = []
    if skew > timedelta(hours=1):
        findings.append(Finding("clock", "crit", prompts.ALERT_CLOCK_SKEW.format(skew=fmt_duration(skew)), clear=2))
    elif skew > retention.MAX_SKEW:         # the nightly purge refuses to run
        findings.append(Finding("clock", "warn", prompts.ALERT_CLOCK_SKEW.format(skew=fmt_duration(skew)), clear=2))
    elif skew > timedelta(minutes=2):
        findings.append(Finding("clock", "warn", prompts.ALERT_CLOCK_DRIFT.format(skew=fmt_duration(skew)), clear=2))
    if await ctx.host.synced():
        ctx.memo("clock").pop("unsynced_since", None)
    else:
        since = ctx.since("clock", "unsynced_since")
        if ctx.now - since >= UNSYNCED_FOR and not findings:
            findings.append(Finding("clock", "warn", prompts.ALERT_CLOCK_UNSYNCED.format(since=ctx.fmt(since)), clear=2))
    return result("clock", findings)


async def check_migrations(ctx: Ctx) -> CheckResult:
    files = [p.stem for p in migration_files()]
    applied = {r["version"] for r in await ctx.conn.fetch("SELECT version FROM schema_migrations")}
    pending = [v for v in files if v not in applied]
    extra = sorted(applied - set(files))
    findings = []
    if pending:
        try:
            unit = await ctx.unit(COLLECTOR)
        except ProbeError:
            unit = {}
        # The collector migrates before it starts (ExecStartPre): a migration that fails keeps it restarting.
        crashing = unit.get("SubState") == "auto-restart" or unit.get("ActiveState") == "failed"
        text = prompts.ALERT_MIGRATION_CRASH if crashing else prompts.ALERT_MIGRATIONS
        findings.append(Finding("migrations.pending", "warn", text.format(versions=html.escape(", ".join(pending))),
                                confirm=2))
    if extra:
        findings.append(Finding("migrations.unknown", "warn",
                                prompts.ALERT_MIGRATIONS_UNKNOWN.format(versions=html.escape(", ".join(extra))),
                                confirm=2))
    return result("migrations", findings)


# ---- the collector ----

async def check_collector(ctx: Ctx) -> CheckResult:
    """collector.down from systemd; collector.restarts, an event, from the NRestarts it counts."""
    unit = await ctx.unit(COLLECTOR)
    restarts, base = as_int(unit.get("NRestarts")), ctx.state.baselines.get("collector_restarts")
    db_down = ctx.conn is None or ctx.is_open("db.unreachable")
    events = []
    if restarts is not None:
        # While the database is down the collector cannot start: those restarts are its fault, said already.
        if base is not None and restarts > base and not db_down:
            reason = prompts.RESTART_REASONS.get(unit.get("Result")) or prompts.RESTART_UNKNOWN
            events.append(Event("collector.restarts",
                                prompts.ALERT_COLLECTOR_RESTARTS.format(count="{count}", reason=reason),
                                count=restarts - base))
        ctx.state.baselines["collector_restarts"] = restarts    # a drop (started by hand) counts from there
    if db_down:
        return unknown("collector")
    active = unit.get("ActiveState")
    if active == "active":
        ctx.memo("collector").clear()
        return result("collector", events=events)
    state = f"{active}/{unit.get('SubState')}" + (f", {unit['Result']}" if unit.get("Result") not in (None, "success")
                                                  else "")
    line = prompts.ALERT_COLLECTOR_DOWN.format(since=ctx.fmt(ctx.since("collector", "down_since")),
                                               state=_code(state))
    return result("collector", [Finding("collector.down", "crit", line, confirm=2)], events)


async def check_heartbeat(ctx: Ctx) -> CheckResult:
    """collector.hung (it runs but does not write) and telegram.polling (it gets no updates), from its
    heartbeat. Only while the unit is active: otherwise collector.down speaks."""
    if (await ctx.unit(COLLECTOR)).get("ActiveState") != "active":
        return unknown("heartbeat")
    beat = await ctx.heartbeat()
    if beat is None:                # a collector older than its telemetry
        if not ctx.memo("heartbeat").get("missing"):
            log.warning("collector heartbeat missing: collector.hung and telegram.polling are not checked")
        ctx.memo("heartbeat")["missing"] = True
        return unknown("heartbeat")
    ctx.memo("heartbeat").pop("missing", None)
    if ctx.db_now - beat["updated_at"] > HUNG_AFTER:
        line = prompts.ALERT_COLLECTOR_HUNG.format(since=ctx.fmt(beat["updated_at"]))
        return result("heartbeat", [Finding("collector.hung", "crit", line, confirm=2)], unknown=("telegram.polling",))
    value = beat["value"] or {}
    failing = isinstance(value.get("updates_failures"), int) and value["updates_failures"] > 0
    error = value.get("updates_error") if failing else None
    if error == "network":
        ctx.memo("heartbeat")["network_at"] = ctx.now.isoformat()
    last = _when(value.get("updates_ok_at")) or _when(value.get("started_at"))
    if error == "401":
        finding = Finding("telegram.polling", "crit", prompts.ALERT_TOKEN_REVOKED, clear=2)
    elif error == "409":
        finding = Finding("telegram.polling", "crit", prompts.ALERT_POLLING_CONFLICT, clear=2)
    elif last is not None and ctx.now - last > POLL_STALE:
        line = prompts.ALERT_POLLING.format(since=ctx.fmt(last), error=_code(safe_code(error) if error else "—"))
        finding = Finding("telegram.polling", "crit", line, confirm=2, clear=2)
    else:
        return result("heartbeat")
    return result("heartbeat", [finding])


# ---- Hermes and the model ----

async def check_hermes(ctx: Ctx) -> CheckResult:
    profiles = hermes_profiles(ctx.settings)
    if not profiles:                # archiving only
        return result("hermes")
    outcomes = await probe_hermes(ctx.http, profiles)
    down = sorted(o for o in outcomes.values() if o in prompts.HERMES_ERRORS)
    if down:                        # one gateway serves every profile: one line, the profiles unknown
        line = prompts.ALERT_HERMES_DOWN.format(error=prompts.HERMES_ERRORS[down[0]])
        return result("hermes", [Finding("hermes.gateway", "crit", line, confirm=2, clear=2)],
                      unknown=("hermes.profile:",))
    memo = ctx.memo("hermes")
    if all(o == "http_404" for o in outcomes.values()):
        if not memo.get("no_models"):
            log.warning("hermes answers 404 to /models on every profile: profiles are not checked")
        memo["no_models"] = True
        # It answers, so the gateway is up; whether a profile works cannot be told from it.
        return result("hermes", unknown=("hermes.profile:",))
    memo.pop("no_models", None)
    findings = []
    for name, outcome in sorted(outcomes.items()):
        if outcome == "ok":
            continue
        code = html.escape(outcome.removeprefix("http_"))
        problem = (prompts.HERMES_KEY.format(code=code) if outcome in ("http_401", "http_403")
                   else prompts.HERMES_MISSING if outcome == "http_404" else prompts.HERMES_HTTP.format(code=code))
        findings.append(Finding(f"hermes.profile:{name}", "crit",
                                prompts.ALERT_HERMES_PROFILE.format(profile=html.escape(name), problem=problem),
                                confirm=2, clear=2))
    return result("hermes", findings)


async def check_model(ctx: Ctx) -> CheckResult:
    """model.errors: answers that failed in the last hour (a spending limit, an outage at Anthropic)."""
    row = await ctx.conn.fetchrow(
        """SELECT count(*) FILTER (WHERE status IN ('error', 'timeout')) AS failed,
                  count(*) FILTER (WHERE model IS NOT NULL OR status IN ('error', 'timeout')) AS total
           FROM bot_requests WHERE created_at > now() - interval '1 hour' AND scenario IS DISTINCT FROM 'export'""")
    if row["failed"] >= 3 and row["failed"] * 2 >= row["total"]:
        line = prompts.ALERT_MODEL_ERRORS.format(failed=row["failed"], total=row["total"])
        return result("model", [Finding("model.errors", "warn", line)])
    return result("model")


# ---- backups and retention ----

async def check_backup(ctx: Ctx) -> CheckResult:
    """backup.*, from nestor.backup's status.json. An empty BACKUP_STATUS_FILE: backups are not watched, which
    /status shows as unchecked and the daily summary says."""
    if not ctx.settings.backup_status_file:
        return unknown("backup")
    path = Path(ctx.settings.backup_status_file)
    findings, unknown_keys = [], []
    dumps = path.parent / "dumps"
    if dumps.is_dir() and ctx.host.readable(dumps):
        findings.append(Finding("backup.exposed", "warn", prompts.ALERT_BACKUP_EXPOSED))
    status = read_json(path)
    if status is None:
        return result("backup", findings + [Finding("backup.missing", "crit", prompts.ALERT_BACKUP_MISSING)])
    last_ok, oldest = _when(status.get("last_ok_at")), _when(status.get("oldest_dump_at"))
    last_ok_text = ctx.fmt(last_ok) if last_ok else "—"
    if status.get("running"):
        try:
            active = (await ctx.unit(BACKUP)).get("ActiveState")
        except ProbeError:
            active = None
            unknown_keys.append("backup.interrupted")
        if active is not None and active not in ("activating", "active", "deactivating", "reloading"):
            started = _when(status.get("started_at"))
            line = prompts.ALERT_BACKUP_INTERRUPTED.format(since=ctx.fmt(started) if started else "—")
            findings.append(Finding("backup.interrupted", "crit", line))
    if status.get("ok") is False:
        code = safe_code(status.get("error") or "failed")
        nights = status.get("failed_nights") if isinstance(status.get("failed_nights"), int) else 1
        line = prompts.ALERT_BACKUP_FAILED.format(nights=nights, reason=prompts.BACKUP_REASONS.get(code) or _code(code),
                                                  last_ok=last_ok_text)
        findings.append(Finding("backup.failed", "crit", line, repeat_token=str(nights)))
    if last_ok is not None and ctx.now - last_ok > BACKUP_STALE:
        findings.append(Finding("backup.stale", "crit", prompts.ALERT_BACKUP_STALE.format(last_ok=last_ok_text)))
    if oldest is not None and ctx.now - oldest > timedelta(days=BACKUP_MAX_AGE_DAYS):
        line = prompts.ALERT_BACKUP_OLD.format(days=(ctx.now - oldest).days, max_days=BACKUP_MAX_AGE_DAYS)
        findings.append(Finding("backup.old_copies", "crit", line))
    # A run that made no copy still says what it did with the old ones (the clock, the rotation), but not
    # that a copy was made; whether a copy shrank waits for the next one.
    made = status.get("ok") is True
    if status.get("ok") is False:
        unknown_keys.append("backup.warning:shrank")
        if not status.get("rotated") and not status.get("warnings"):
            unknown_keys.append("backup.warning:")      # it failed before it looked at the old copies
    for warning in status.get("warnings") or ():
        if not isinstance(warning, str) or not re.fullmatch(r"[a-z_]{1,40}", warning):
            continue
        text = prompts.BACKUP_WARNINGS.get(warning) or _code(warning)
        if warning == "shrank":
            pct = status.get("shrank_pct") if isinstance(status.get("shrank_pct"), int) else "?"
            line = prompts.ALERT_BACKUP_SHRANK.format(pct=pct, why=await _purged(ctx))
        else:
            line = (prompts.ALERT_BACKUP_WARNING if made else prompts.ALERT_BACKUP_NOTE).format(warning=text)
        # Said again with each new copy, or each failed night.
        token = status.get("file") if made else status.get("failed_nights")
        findings.append(Finding(f"backup.warning:{warning}", "warn", line,
                                label=prompts.BACKUP_WARNING_LABELS.get(warning, ""), repeat_token=str(token)))
    return result("backup", findings, unknown=unknown_keys)


async def _purged(ctx: Ctx) -> str:
    """Why a dump may have shrunk: what the last nightly purge removed."""
    if ctx.conn is None:
        return ""
    counts = await ctx.conn.fetchval(
        "SELECT counts FROM maintenance_runs WHERE job = $1 AND status = 'ok' ORDER BY day DESC LIMIT 1", retention.JOB)
    count = (counts or {}).get("messages", 0)
    return prompts.ALERT_BACKUP_SHRANK_WHY.format(count=count) if isinstance(count, int) and count > 0 else ""


async def check_retention(ctx: Ctx) -> CheckResult:
    """retention.*: the latest nightly run (maintenance_runs), its age, and anything left past its time."""
    conn, findings = ctx.conn, []
    last = await conn.fetchrow(
        """SELECT day, status, attempts, counts, error, updated_at FROM maintenance_runs WHERE job = $1
           ORDER BY day DESC LIMIT 1""", retention.JOB)
    if last is not None and last["status"] == "blocked":
        counts = last["counts"] or {}
        line = prompts.ALERT_RETENTION_BLOCKED.format(count=int(counts.get("would_delete", 0)),
                                                      total=int(counts.get("total", 0)))
        findings.append(Finding("retention.blocked", "warn", line, repeat_token=str(last["day"])))
    # A failed run is claimed again after 10 minutes, up to 6 times: only one that stays failed is news.
    if last is not None and last["status"] == "failed" and (
            last["attempts"] >= 6 or ctx.db_now - last["updated_at"] > timedelta(minutes=20)):
        code = safe_code(last["error"] or "failed")
        reason = prompts.RETENTION_REASONS.get(code) or prompts.RETENTION_ERROR.format(error=html.escape(code))
        findings.append(Finding("retention.failed", "warn", prompts.ALERT_RETENTION_FAILED.format(reason=reason),
                                repeat_token=str(last["day"])))
    since = await conn.fetchval("SELECT max(finished_at) FROM maintenance_runs WHERE job = $1 AND status = 'ok'",
                                retention.JOB)
    if since is None:               # never ran: count from the day it could have
        since = await conn.fetchval("SELECT applied_at FROM schema_migrations WHERE version = $1", RETENTION_MIGRATION)
    if since is not None and ctx.db_now - since > RETENTION_STALE:
        findings.append(Finding("retention.stale", "warn", prompts.ALERT_RETENTION_STALE.format(since=ctx.fmt(since))))
    late = {k: v for k, v in (await retention.overdue(conn)).items() if v}
    if late:
        what = ", ".join(f"{prompts.RETENTION_KINDS.get(k, html.escape(k))}: {v}" for k, v in late.items())
        findings.append(Finding("retention.overdue", "crit",
                                prompts.ALERT_RETENTION_OVERDUE.format(what=what, months=RETENTION_MONTHS)))
    return result("retention", findings)


# ---- the Pi ----

async def check_disk(ctx: Ctx) -> CheckResult:
    usage = ctx.host.disk_usage("/")
    warn_below = ctx.settings.alert_disk_min_gb * GIB
    if ctx.is_open("disk.low:/"):
        warn_below += GIB           # it clears a gigabyte above where it began, not on every wobble
    level = "crit" if usage.free < GIB else "warn" if usage.free < warn_below else None
    if level is None:
        return result("disk")
    line = prompts.ALERT_DISK.format(free=fmt_size(usage.free), total=fmt_size(usage.total))
    return result("disk", [Finding("disk.low:/", level, line)])


def card_result(state_dir: Path) -> CheckResult:
    """sd.write: writes and fsyncs a probe file where the state lives. A card that went read-only or
    fails to write is found on the run it happens, whether or not the state changed."""
    try:
        fd = os.open(state_dir / "probe", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, str(time.time()).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        error = errno.errorcode.get(e.errno, type(e).__name__) if e.errno else type(e).__name__
        return result("sd", [Finding("sd.write", "crit", prompts.ALERT_SD_WRITE.format(error=_code(error)))])
    return result("sd")


# ---- reminders and news ----

async def check_reminders(ctx: Ctx) -> CheckResult:
    row = await ctx.conn.fetchrow(
        """SELECT count(*) AS n, min(r.due_at) AS oldest
           FROM reminders r JOIN chats c ON c.chat_id = r.chat_id AND c.enabled
           WHERE r.sent_at IS NULL AND r.cancelled_at IS NULL AND r.due_at < now() - interval '15 minutes'""")
    if not row["n"]:
        return result("reminders")
    line = prompts.ALERT_REMINDERS_LATE.format(count=row["n"], ago=fmt_duration(ctx.db_now - row["oldest"]))
    return result("reminders", [Finding("reminders.late", "warn", line, confirm=2)])


async def failing_sources(conn: asyncpg.Connection, db_now: datetime) -> tuple[list[asyncpg.Record], int]:
    """News sources read in the last 2 days that failed 3 times in a row and had nothing good for a day;
    and how many sources were read."""
    rows = await conn.fetch("""SELECT key, status, failures, last_ok_at FROM news_sources
                               WHERE last_run_at > now() - interval '2 days' ORDER BY key""")
    failing = [r for r in rows if r["failures"] >= 3
               and (r["last_ok_at"] is None or db_now - r["last_ok_at"] > NEWS_FAILING_FOR)]
    return failing, len(rows)


async def check_news(ctx: Ctx) -> CheckResult:
    """news.sources: one key for all of them, raised only when half or more fail; fewer are in the summary."""
    network = _when(ctx.memo("heartbeat").get("network_at"))
    if network is not None and ctx.now - network < NEWS_AFTER_NETWORK:
        return unknown("news")
    failing, total = await failing_sources(ctx.conn, ctx.db_now)
    if not total or len(failing) * 2 < total:
        return result("news")
    names = ", ".join(html.escape(r["key"]) for r in failing[:5]) + (", …" if len(failing) > 5 else "")
    line = prompts.ALERT_NEWS.format(count=len(failing), total=total, names=names)
    return result("news", [Finding("news.sources", "warn", line)])


CHECKS: tuple[Check, ...] = (
    Check("clock", check_clock, needs_db=True),
    Check("migrations", check_migrations, needs_db=True),
    Check("collector", check_collector),
    Check("heartbeat", check_heartbeat, needs_db=True),
    Check("hermes", check_hermes),
    Check("model", check_model, every=timedelta(minutes=15), needs_db=True),
    Check("backup", check_backup, every=timedelta(minutes=15)),
    Check("retention", check_retention, every=timedelta(minutes=60), needs_db=True),
    Check("disk", check_disk, every=timedelta(minutes=15)),
    Check("reminders", check_reminders, needs_db=True),
    Check("news", check_news, every=timedelta(minutes=60), needs_db=True),
)
