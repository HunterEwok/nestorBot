"""Check the music charts from this machine, without the database and without any model.

    .venv/bin/python -m nestor.trends --check              # every chart: answered or not, size, top 3
    .venv/bin/python -m nestor.trends --check -v yandex_ru # every entry with its move and marks

Reads LASTFM_API_KEY, MUSIC_YANDEX_PROXY and NEWS_CONTACT_EMAIL from .env; without the key the Last.fm chart is skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from ..news.http import Http, HttpError
from .sources import ADAPTERS, CHARTS, Entry, Options, artist_key


def _line(e: Entry) -> str:
    marks = [f"{e.shift:+d}" if e.shift else "", "новинка" if e.is_new else ""]
    marks = ", ".join(m for m in marks if m)
    return f"    {e.position:>3}. {e.artist} — {e.title}" + (f"  [{marks}]" if marks else "") + f"  ({artist_key(e.artist)})"


async def check(keys: list[str], verbose: bool = False) -> int:
    key = os.environ.get("LASTFM_API_KEY", "").strip()
    opts = Options(key, os.environ.get("MUSIC_YANDEX_PROXY", "").strip())
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
                print(f"✗ {chart.key}: {type(e).__name__}: {str(e)[:120]}")
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


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m nestor.trends")
    parser.add_argument("--check", action="store_true", required=True, help="read the charts and print them")
    parser.add_argument("-v", "--verbose", action="store_true", help="every entry")
    parser.add_argument("charts", nargs="*", help="chart keys, all by default")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.charts, args.verbose)))


if __name__ == "__main__":
    main()
