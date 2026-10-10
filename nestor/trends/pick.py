"""The two model calls of a recommendation: the cheap check of who is Russian and who is already
a star, and the write-up with the best song. Links come from the charts and from music.build_card,
never from the model's text."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..bot import music, prompts
from ..bot.llm import extract_json
from ..bot.reminders import MONTHS
from .score import Candidate

MAX_ITEMS = 3
MAX_TEXT = 400


def check_prompt(candidates: list[Candidate]) -> list[dict]:
    lines = [json.dumps({"n": i, "artist": c.artist, "song": c.title}, ensure_ascii=False)
             for i, c in enumerate(candidates, 1)]
    return [{"role": "system", "content": prompts.MUSIC_CHECK_SYSTEM},
            {"role": "user", "content": "Исполнители:\n" + "\n".join(lines)}]


@dataclass(frozen=True)
class Verdict:
    russian: bool
    known: bool


def parse_check(answer: str, count: int) -> dict[int, Verdict]:
    """n -> verdict, for the answers that name a candidate and both booleans."""
    data = extract_json(answer)
    items = data.get("items") if isinstance(data, dict) else None
    out: dict[int, Verdict] = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        n, russian, known = it.get("n"), it.get("russian"), it.get("known")
        if type(n) is int and 1 <= n <= count and isinstance(russian, bool) and isinstance(known, bool):
            out.setdefault(n, Verdict(russian, known))
    return out


def write_prompt(candidates: list[Candidate], day: date) -> list[dict]:
    lines = [f"[{i}] {c.artist} — «{c.title}». " + "; ".join(c.facts) for i, c in enumerate(candidates, 1)]
    user = f"Сегодня {day.day} {MONTHS[day.month - 1]} {day.year}. Кандидаты:\n" + "\n".join(lines)
    return [{"role": "system", "content": prompts.MUSIC_TRENDS_SYSTEM}, {"role": "user", "content": user}]


@dataclass(frozen=True)
class Plan:
    items: list[tuple[int, str]]    # (candidate number, text)
    best: music.Pick | None


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def parse_write(answer: str, candidates: list[Candidate]) -> Plan | None:
    """None: no JSON at all. An empty plan: the model found nobody worth it."""
    data = extract_json(answer)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    items: list[tuple[int, str]] = []
    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        n, text = it.get("n"), _text(it.get("text"), MAX_TEXT)
        if type(n) is int and 1 <= n <= len(candidates) and len(text) >= 15 and n not in {i for i, _ in items}:
            items.append((n, text))
    items = items[:MAX_ITEMS]
    if not items:
        return Plan([], None)
    best = data.get("best") if isinstance(data.get("best"), dict) else {}
    n = best.get("n") if best.get("n") in {i for i, _ in items} else items[0][0]
    c = candidates[n - 1]
    title = _text(best.get("title"), 150) if best.get("n") == n else ""
    raw = best.get("youtube") if best.get("n") == n else None
    raw = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    ids: list[str] = []
    for url in raw[:6]:
        vid = music.youtube_id(url)
        if vid and vid not in ids:
            ids.append(vid)
    spotify = music.spotify_track_id(best.get("spotify")) if best.get("n") == n else None
    return Plan(items, music.Pick(c.artist, title or c.title, tuple(ids[:3]), spotify))


def render(plan: Plan, candidates: list[Candidate], day: date, card: music.Card) -> tuple[str, str]:
    """(Telegram HTML, plain text) of the post: the artists, then the best song's card."""
    head = prompts.MUSIC_TRENDS_HEADER.format(date=f"{day.day} {MONTHS[day.month - 1]}")
    lines, plain = [head], [re.sub(r"<[^>]+>", "", head)]
    for n, text in plan.items:
        c = candidates[n - 1]
        song = html.escape(c.title)
        if c.url:
            song = f'<a href="{html.escape(c.url, quote=True)}">{song}</a>'
        lines.append(f"• <b>{html.escape(c.artist)}</b> — «{song}». {html.escape(text)}")
        plain.append(f"• {c.artist} — «{c.title}». {text}" + (f" {c.url}" if c.url else ""))
    body = "\n\n".join(lines) + f"\n\n{prompts.MUSIC_TRENDS_BEST}\n" + card.html
    return body, "\n\n".join(plain) + f"\n\n{prompts.MUSIC_TRENDS_BEST}\n" + card.plain
