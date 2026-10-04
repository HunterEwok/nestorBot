"""Check the news sources from this machine, without the database and without any model.

    .venv/bin/python -m nestor.news --check              # every source
    .venv/bin/python -m nestor.news --check pubmed tg_psychedelicsocietyvienna

For each source: whether it answered, how many entries it gave, how many pass the filters,
the newest date and two titles. Reads NEWS_CONTACT_EMAIL and NEWS_TELEGRAM_CHANNELS from .env.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv

from . import filters
from .adapters import ADAPTERS
from .http import Http, HttpError
from .sources import all_sources, parse_channels


async def check(keys: list[str]) -> int:
    channels = parse_channels(os.environ.get("NEWS_TELEGRAM_CHANNELS", "psychedelicsocietyvienna"))
    sources = [s for s in all_sources(channels) if not keys or s.key in keys]
    unknown = set(keys) - {s.key for s in sources}
    if unknown:
        print("Нет таких источников:", ", ".join(sorted(unknown)))
        print("Есть:", ", ".join(s.key for s in all_sources(channels)))
        return 2
    now = datetime.now(timezone.utc)
    failed = 0
    async with Http(os.environ.get("NEWS_CONTACT_EMAIL", "").strip()) as http:
        for source in sources:
            try:
                result = await asyncio.wait_for(ADAPTERS[source.kind](http, source, {}, now), 300)
            except HttpError as e:
                print(f"✗ {source.key}: HTTP {e.status}")
                failed += 1
                continue
            except Exception as e:  # noqa: BLE001 - report and go on with the next source
                print(f"✗ {source.key}: {type(e).__name__}: {str(e)[:120]}")
                failed += 1
                continue
            reasons = Counter(filters.drop_reason(i, source) for i in result.items)
            passed = [i for i in result.items if filters.drop_reason(i, source) is None]
            dates = [i.published_at for i in result.items if i.published_at]
            newest = max(dates).astimezone().strftime("%Y-%m-%d %H:%M") if dates else "нет дат"
            dropped = ", ".join(f"{r} {n}" for r, n in reasons.most_common() if r)
            note = " (данные не обновлялись)" if result.skipped else ""
            print(f"✓ {source.key}: записей {len(result.items)}, проходят фильтр {len(passed)}, "
                  f"самая новая {newest}{note}" + (f"; отброшено: {dropped}" if dropped else ""))
            for item in passed[:2]:
                print(f"    {item.title[:100]}")
    print(f"\nИсточников: {len(sources)}, с ошибкой: {failed}")
    return 1 if failed else 0


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m nestor.news", description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", nargs="*", metavar="SOURCE", help="проверить источники (все или названные)")
    args = parser.parse_args()
    if args.check is None:
        parser.print_help()
        return
    raise SystemExit(asyncio.run(check(args.check)))


if __name__ == "__main__":
    main()
