"""python -m nestor.watchdog [--test | --dry-run | --mute MIN]

    (no flags)    one run: the timer's unit
    --test        sends only the test line to ALERT_CHAT_ID
    --dry-run     prints the status and the message a run would send; sends and saves nothing
    --mute MIN    for MIN minutes new problems wait and messages arrive without sound; 0 ends it
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot

from ..bot import markup, prompts
from ..config import Settings, check_alert_chat, load_settings
from ..logs import setup_logging
from . import notify
from .alerts import fmt_since, render_status
from .run import STATE_DIR, Busy, connect, keep_optouts, mute, run_once

log = logging.getLogger("nestor.watchdog")


async def _optouts_only(settings: Settings, state_dir: Path) -> None:
    conn, _, _ = await connect(settings.database_url)
    if conn is not None:
        try:
            await keep_optouts(conn, state_dir)
        finally:
            await conn.close()


async def _main(settings: Settings, args: argparse.Namespace, state_dir: Path) -> int:
    bot = Bot(settings.bot_token)
    try:
        if args.test:
            ok = await notify.send(bot, settings.alert_chat_id, prompts.ALERT_TEST, silent=False)
            print("test sent" if ok else "test failed: see the journal")
            return 0 if ok else 1
        async with aiohttp.ClientSession() as http:
            report = await run_once(settings, bot=bot, http=http, state_dir=state_dir, dry_run=args.dry_run)
        if args.dry_run:
            now = datetime.now(UTC)
            print(markup.to_plain(render_status(report.snapshot, now, ZoneInfo(settings.timezone))))
            if report.message is not None:
                print("\n---\n" + markup.to_plain(report.message.text))
        return 0
    except Busy:
        log.info("watchdog already running")
        return 0
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nestor.watchdog", description=__doc__.split("\n\n")[0])
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--test", action="store_true", help="send only the test line")
    what.add_argument("--dry-run", action="store_true", help="print what a run would send; send and save nothing")
    what.add_argument("--mute", type=int, metavar="MIN", help="hold new problems and silence messages for MIN minutes")
    args = parser.parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level)
    state_dir = Path(os.environ.get("STATE_DIRECTORY") or STATE_DIR)    # set by systemd for StateDirectory=
    problem = check_alert_chat(settings)
    if problem:
        if not (args.test or args.dry_run) and args.mute is None:
            asyncio.run(_optouts_only(settings, state_dir))     # nobody can be told; a restore still needs them
        raise SystemExit(problem)
    if args.mute is not None:
        try:
            until = mute(state_dir, args.mute)
        except OSError as e:
            raise SystemExit(f"cannot write the watchdog state in {state_dir}: {type(e).__name__}") from None
        tz = ZoneInfo(settings.timezone)
        print(f"muted until {fmt_since(until, datetime.now(UTC), tz)}" if until else "unmuted")
        return 0
    if settings.alert_chat_id is None and not args.dry_run:
        if args.test:
            raise SystemExit("ALERT_CHAT_ID is empty: there is nowhere to send the test")
        # Nobody to tell; the opt-outs are still kept for a restore.
        log.warning("alerts are off: ALERT_CHAT_ID is empty")
        asyncio.run(_optouts_only(settings, state_dir))
        return 0
    return asyncio.run(_main(settings, args, state_dir))


if __name__ == "__main__":
    raise SystemExit(main())
