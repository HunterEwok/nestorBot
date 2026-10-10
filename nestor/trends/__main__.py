"""The music charts from the command line, without any model.

    .venv/bin/python -m nestor.trends --check              # every chart: answered or not, size, top 3
    .venv/bin/python -m nestor.trends --check -v yandex_ru # every entry with its move and marks
    .venv/bin/python -m nestor.trends --backfill           # copy half a year of history from the Wayback Machine
    .venv/bin/python -m nestor.trends --backtest 60        # the song of the day the bot would have picked

--check needs no database. --backfill and --backtest use DATABASE_URL; --backtest only reads.
Reads LASTFM_API_KEY, MUSIC_YANDEX_PROXY and NEWS_CONTACT_EMAIL from .env; without the key the Last.fm chart is skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from ..config import _env, _proxy
from ..news.http import Http, HttpError
from . import store, viral
from .sources import ADAPTERS, CHARTS, Entry, Options, artist_key


def _line(e: Entry) -> str:
    marks = [f"{e.shift:+d}" if e.shift else "", "новинка" if e.is_new else ""]
    marks = ", ".join(m for m in marks if m)
    return f"    {e.position:>3}. {e.artist} — {e.title}" + (f"  [{marks}]" if marks else "") + f"  ({artist_key(e.artist)})"


async def check(keys: list[str], verbose: bool = False) -> int:
    key = os.environ.get("LASTFM_API_KEY", "").strip()
    opts = Options(key, _proxy("MUSIC_YANDEX_PROXY"))    # the bot's own check: a bad value stops here, unechoed
    charts = [c for c in CHARTS if not keys or c.key in keys]
    unknown = set(keys) - {c.key for c in charts}
    if unknown:
        print("Нет таких чартов:", ", ".join(sorted(unknown)))
        print("Есть:", ", ".join(c.key for c in CHARTS))
        return 2
    failed = 0
    now = datetime.now(timezone.utc)
    async with Http(os.environ.get("NEWS_CONTACT_EMAIL", "").strip()) as http:
        for chart in charts:
            if chart.needs_key and not key:
                print(f"– {chart.key}: пропущен, нет LASTFM_API_KEY")
                continue
            try:
                entries = await asyncio.wait_for(ADAPTERS[chart.kind](http, now, opts), 120)
            except HttpError as e:
                why = {451: " (сервис не отдаёт чарт из этой страны; поможет MUSIC_YANDEX_PROXY с российским адресом)",
                       403: " (сервис отказал)"}.get(e.status, "")
                print(f"✗ {chart.key}: HTTP {e.status}{why}")
                failed += 1
                continue
            except Exception as e:  # noqa: BLE001 - report and go on with the next chart
                text = str(e).replace(opts.yandex_proxy, "<MUSIC_YANDEX_PROXY>") if opts.yandex_proxy else str(e)
                print(f"✗ {chart.key}: {type(e).__name__}: {text[:120]}")
                failed += 1
                continue
            moved = sum(1 for e in entries if e.shift)
            new = sum(1 for e in entries if e.is_new)
            print(f"{'✓' if entries else '✗'} {chart.key}: мест {len(entries)}, со сдвигом {moved}, новинок {new}")
            failed += not entries
            for e in entries if verbose else entries[:3]:
                print(_line(e))
    print(f"\nЧартов: {len(charts)}, с ошибкой: {failed}")
    return 1 if failed else 0


def _today() -> date:
    return datetime.now(ZoneInfo(_env("TIMEZONE", "Europe/Moscow"))).date()


async def _connect():
    import asyncpg

    return await asyncpg.connect(_env("DATABASE_URL"))


async def run_backfill(days: int) -> int:
    from .backfill import backfill

    conn = await _connect()
    try:
        def progress(key: str, added: int, total: int) -> None:
            print(f"\r{key}: {added} из {total} снимков", end="", flush=True)

        async with Http(os.environ.get("NEWS_CONTACT_EMAIL", "").strip()) as http:
            added = await backfill(conn, http, _today(), days, progress)
        print()
        for key, n in added.items():
            print(f"{key}: добавлено дней {n}")
        return 0
    finally:
        await conn.close()


async def backtest(days: int, threshold: float, top: int) -> int:
    """For each of the last `days` days: the song the bot would have picked from what it knew by then.
    The cooldowns are simulated with these picks; nothing is written."""
    conn = await _connect()
    try:
        today = _today()
        rows = await store.history(conn, today - timedelta(days=days + viral.HISTORY_DAYS))
        banned = await store.banned(conn)
    finally:
        await conn.close()
    have = sorted({r["day"] for r in rows})
    print(f"В базе дней с чартами: {len(have)}" + (f", с {have[0]:%d.%m.%Y} по {have[-1]:%d.%m.%Y}" if have else ""))
    picks: list[viral.Pick] = []
    found_days = 0
    for n in range(days, -1, -1):
        day = today - timedelta(days=n)
        if day not in have:
            continue
        found = viral.viral(rows, day, picks=picks, banned=banned, threshold=threshold, limit=top)
        if not found:
            print(f"{day:%d.%m.%Y}  —  вирусной песни нет")
            continue
        found_days += 1
        c = found[0]
        picks.append(viral.Pick(day, c.key, c.artist_key))
        print(f"{day:%d.%m.%Y}  {c.artist} — {c.title}  [{c.score}]  " + "; ".join(c.facts))
        for other in found[1:]:
            print(f"            также: {other.artist} — {other.title} [{other.score}]")
    print(f"\nДней с вирусной песней: {found_days}. Порог {threshold}: поднять — реже и строже, опустить — чаще.")
    return 0


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m nestor.trends")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read the charts and print them")
    mode.add_argument("--backfill", type=int, nargs="?", const=183, metavar="DAYS",
                      help="copy the charts' history from the Wayback Machine (default 183 days)")
    mode.add_argument("--backtest", type=int, metavar="DAYS", help="the song of the day for each of the last DAYS")
    parser.add_argument("--threshold", type=float, default=viral.THRESHOLD, help="the backtest's score threshold")
    parser.add_argument("--top", type=int, default=1, help="the backtest's songs per day")
    parser.add_argument("-v", "--verbose", action="store_true", help="every entry")
    parser.add_argument("charts", nargs="*", help="chart keys, all by default")
    args = parser.parse_args()
    if args.backfill is not None:
        raise SystemExit(asyncio.run(run_backfill(args.backfill)))
    if args.backtest is not None:
        raise SystemExit(asyncio.run(backtest(args.backtest, args.threshold, args.top)))
    raise SystemExit(asyncio.run(check(args.charts, args.verbose)))


if __name__ == "__main__":
    main()
