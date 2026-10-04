"""Check the news sources from this machine, without the database and without any model.

    .venv/bin/python -m nestor.news --check              # every source
    .venv/bin/python -m nestor.news --check pubmed tg_psychedelicsocietyvienna
    .venv/bin/python -m nestor.news --check -v reddit_psychonaut    # every entry with its verdict

For each source: whether it answered, how many entries it gave, how many pass the filters,
the newest date and two titles. With -v, every entry instead of the two titles: the filters'
verdict (a drop reason or "ok"), its date and title, and what the verdict rests on: score,
comments, self or link post, the archive's removal category, whether the archive has looked at
the post again and the link's domain for Reddit; the forward for Telegram; the change label for
trials; the version for preprints.
Reads NEWS_CONTACT_EMAIL and NEWS_TELEGRAM_CHANNELS from .env.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from . import filters
from .adapters import ADAPTERS
from .http import Http, HttpError
from .model import Item, Source
from .sources import all_sources, parse_channels


async def check(keys: list[str], verbose: bool = False) -> int:
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
            verdicts = [(i, filters.drop_reason(i, source)) for i in result.items]
            reasons = Counter(r for _, r in verdicts)
            passed = [i for i, r in verdicts if r is None]
            dates = [i.published_at for i in result.items if i.published_at]
            newest = max(dates).astimezone().strftime("%Y-%m-%d %H:%M") if dates else "нет дат"
            dropped = ", ".join(f"{r} {n}" for r, n in reasons.most_common() if r)
            note = " (данные не обновлялись)" if result.skipped else ""
            print(f"✓ {source.key}: записей {len(result.items)}, проходят фильтр {len(passed)}, "
                  f"самая новая {newest}{note}" + (f"; отброшено: {dropped}" if dropped else ""))
            if not verbose:
                for item in passed[:2]:
                    print(f"    {item.title[:100]}")
                continue
            if source.kind == "ctgov" and result.items:
                print("    (здесь нет истории статусов: смена статуса показана как update)")
            for item, reason in verdicts:
                print(_line(item, source, reason))
    print(f"\nИсточников: {len(sources)}, с ошибкой: {failed}")
    return 1 if failed else 0


def _line(item: Item, source: Source, reason: str | None) -> str:
    when = item.published_at.astimezone().strftime("%Y-%m-%d %H:%M") if item.published_at else "без даты".ljust(16)
    facts = _facts(item, source)
    return f"    {reason or 'ok':<12} {when}  {item.title[:100]}" + (f"  [{facts}]" if facts else "")


def _facts(item: Item, source: Source) -> str:
    """The meta a verdict rests on, as the adapter keeps it."""
    meta: dict[str, Any] = item.meta or {}
    out: list[str] = []
    if source.kind == "arctic":
        out.append(f"score {meta.get('score')}, comments {meta.get('num_comments')}")
        out.append("self" if meta.get("is_self") else "link")
        if meta.get("removed_by"):
            later = (", approved later" if meta.get("restored")
                     else ", removed later" if meta.get("removed_later") else "")
            out.append(f"removed_by {meta['removed_by']}{later}")
        elif meta.get("removed_later"):
            out.append("removed later")         # _meta.was_deleted_later without a category
        elif meta.get("removed"):
            out.append("body removed")          # selftext "[removed]" or "[deleted]" without a category
        if meta.get("rechecked") is False:
            out.append("not rechecked")         # the archive's second look, about 36 h after posting, is still due
        out.append(f"domain {filters._link_domain(item) or '-'}")
    elif source.kind == "tme" and meta.get("forwarded"):
        out.append(f"forwarded from {meta.get('forwarded_from') or '?'}")
    if "change" in meta:
        out.append(f"{meta['change']}, {meta.get('status') or '?'}")
    if "version" in meta:
        out.append(f"v{meta['version']}" + (", v1 in the window" if meta.get("first_in_window") else ""))
    if meta.get("pubtypes"):
        out.append(", ".join(str(p) for p in list(meta["pubtypes"])[:3]))
    return "; ".join(out)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m nestor.news", description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="проверить источники (все или названные)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="с --check: каждая запись с причиной отсева, датой и главным из meta")
    parser.add_argument("sources", nargs="*", metavar="SOURCE", help="ключи источников, например pubmed")
    args = parser.parse_intermixed_args(argv)
    if not args.check:
        parser.print_help()
    return args


def main() -> None:
    load_dotenv()
    args = parse_args()
    if not args.check:
        return
    raise SystemExit(asyncio.run(check(args.sources, args.verbose)))


if __name__ == "__main__":
    main()
