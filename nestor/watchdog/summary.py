"""The silent daily summary at ALERT_DAILY_AT: counts only, never a message's words. It is also the
dead man's switch: a morning without it means the watchdog, the Pi or the bot token is gone.

Only for the people the alert group is meant for: with more members than ALERT_MAX_MEMBERS (the bot
counts) it says only that, and nothing else."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .. import retention
from ..bot import prompts
from ..bot.daily import day_name
from .alerts import confirmed, fmt_size
from .checks import COLLECTOR, Ctx, failing_sources
from .probes import ProbeError, as_int, read_json, safe_status

log = logging.getLogger("nestor.watchdog")

MEMORY_HIGH = 240 * 1024 ** 2
MAX_CHATS = 5
MAX_PROBLEMS = 10
MAX_SOURCES = 10
MAX_TEXT = 3900          # Telegram takes 4096


async def members(bot: Bot, chat_id: int) -> int | None:
    try:
        return await bot.get_chat_member_count(chat_id)
    except TelegramAPIError as e:
        log.warning("alert group members unknown", extra={"error": type(e).__name__})
        return None


async def daily_summary(conn: asyncpg.Connection | None, ctx: Ctx, bot: Bot) -> str | None:
    """The summary of yesterday (local), or None to try again on the next run (the members could not be
    counted). The caller holds a read-only transaction on `conn`; None: the database is unreachable."""
    count = await members(bot, ctx.settings.alert_chat_id)
    if count is None:
        return None
    if count > ctx.settings.alert_max_members:
        return prompts.ALERT_STRANGERS.format(count=count)
    day = ctx.now.astimezone(ctx.tz).date() - timedelta(days=1)
    start = datetime.combine(day, datetime.min.time(), ctx.tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), ctx.tz)
    lines = [prompts.DAILY_HEADER.format(date=day_name(day))]
    if conn is None:
        lines.append(prompts.DAILY_DB_DOWN)
    else:
        lines += await _database(conn, ctx, start, end)
    lines += _backup(ctx)
    usage = ctx.host.disk_usage("/")
    lines.append(prompts.DAILY_DISK.format(free=fmt_size(usage.free), total=fmt_size(usage.total)))
    lines += await _collector(ctx)
    if conn is not None:
        failing, _ = await failing_sources(conn, ctx.db_now)
        if failing:
            names = ", ".join(f"{html.escape(r['key'])} ({safe_status(r['status'])})" for r in failing[:MAX_SOURCES])
            lines.append(prompts.DAILY_NEWS.format(names=names + (", …" if len(failing) > MAX_SOURCES else "")))
    problems = confirmed(ctx.state)
    if problems:
        lines.append(prompts.DAILY_PROBLEMS.format(count=len(problems)))
        size, shown = sum(len(line) + 1 for line in lines), 0
        for p in problems[:MAX_PROBLEMS]:
            line = f"{prompts.ALERT_MARKS[p.level]} {p.line}"
            size += len(line) + 1
            if size > MAX_TEXT:
                break
            lines.append(line)
            shown += 1
        if len(problems) > shown:
            lines.append(prompts.ALERT_MORE.format(count=len(problems) - shown))
    else:
        lines.append(prompts.DAILY_NO_PROBLEMS)
    return "\n".join(lines)


async def _database(conn: asyncpg.Connection, ctx: Ctx, start: datetime, end: datetime) -> list[str]:
    chats = await conn.fetch(
        """SELECT c.title, count(*) AS n FROM messages m JOIN chats c USING (chat_id)
           WHERE m.stored_at >= $1 AND m.stored_at < $2 GROUP BY c.chat_id, c.title ORDER BY n DESC, c.chat_id""",
        start, end)
    size = fmt_size(await conn.fetchval("SELECT pg_database_size(current_database())"))
    if chats:
        listed = ", ".join(f"«{html.escape(r['title'] or '?')}» {r['n']}" for r in chats[:MAX_CHATS])
        lines = [prompts.DAILY_ARCHIVE.format(chats=listed + (", …" if len(chats) > MAX_CHATS else ""), size=size)]
    else:
        lines = [prompts.DAILY_ARCHIVE_EMPTY.format(size=size)]
    row = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE status = 'ok' AND trigger_msg_id IS NOT NULL) AS answers,
                  count(*) FILTER (WHERE status IN ('error', 'timeout')) AS errors,
                  coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS tokens
           FROM bot_requests WHERE created_at >= $1 AND created_at < $2""", start, end)
    limit = ctx.settings.daily_token_limit
    lines.append(prompts.DAILY_ANSWERS.format(answers=row["answers"], errors=row["errors"], tokens=row["tokens"],
                                              limit=limit, pct=round(100 * row["tokens"] / limit) if limit else 0))
    counts = await conn.fetchval(
        "SELECT counts FROM maintenance_runs WHERE job = $1 AND status = 'ok' ORDER BY day DESC LIMIT 1", retention.JOB)
    ahead = (await retention.preview(conn, ctx.db_now + timedelta(days=7)))["messages"]
    lines.append(prompts.DAILY_PURGE.format(count=int((counts or {}).get("messages", 0)), next=ahead))
    return lines


def _backup(ctx: Ctx) -> list[str]:
    if not ctx.settings.backup_status_file:
        return []
    try:
        status = read_json(Path(ctx.settings.backup_status_file)) or {}
    except (OSError, ValueError, TypeError):
        return []                   # the backup check says why
    try:
        last_ok = datetime.fromisoformat(status["last_ok_at"])
        oldest = datetime.fromisoformat(status["oldest_dump_at"])
    except (KeyError, TypeError, ValueError):
        return [prompts.DAILY_BACKUP_NONE]
    size = status.get("last_ok_size")
    return [prompts.DAILY_BACKUP.format(time=ctx.fmt(last_ok), size=fmt_size(size) if isinstance(size, int) else "?",
                                        kept=status.get("kept", "?"), oldest=day_name(oldest.astimezone(ctx.tz).date()))]


async def _collector(ctx: Ctx) -> list[str]:
    try:
        unit = await ctx.unit(COLLECTOR)
    except ProbeError:
        return []
    peak, restarts = as_int(unit.get("MemoryPeak")), as_int(unit.get("NRestarts"))
    if restarts is not None:
        seen = ctx.state.baselines.get("summary_restarts")
        # NRestarts counts from the last start by hand: a drop means it began again from 0.
        ctx.summary_restarts = restarts
        restarts = restarts - seen if seen is not None and seen <= restarts else restarts
    memory = (fmt_size(peak) + (prompts.DAILY_MEMORY_HIGH if peak > MEMORY_HIGH else "")) if peak is not None else "?"
    return [prompts.DAILY_COLLECTOR.format(memory=memory, restarts=restarts if restarts is not None else "?")]
