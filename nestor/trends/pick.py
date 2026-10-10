"""The model call of the song of the day: a few lines on why it went viral and where to listen.
The song is chosen by code (viral.py); links come from the charts and from music.build_card,
never from the model's text."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..bot import music, prompts
from ..bot.llm import extract_json
from ..bot.reminders import MONTHS
from .viral import Candidate

MAX_TEXT = 500


def write_prompt(c: Candidate, day: date) -> list[dict]:
    user = (f"Сегодня {day.day} {MONTHS[day.month - 1]} {day.year}. Песня: {c.artist} — «{c.title}». "
            "Рост в чартах: " + "; ".join(c.facts))
    return [{"role": "system", "content": prompts.MUSIC_VIRAL_SYSTEM}, {"role": "user", "content": user}]


@dataclass(frozen=True)
class Plan:
    text: str
    best: music.Pick


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def parse_write(answer: str, c: Candidate) -> Plan | None:
    """None: no JSON at all. The text may be empty: the post then carries the growth alone."""
    data = extract_json(answer)
    if not isinstance(data, dict):
        return None
    raw = data.get("youtube")
    raw = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    ids: list[str] = []
    for url in raw[:6]:
        vid = music.youtube_id(url)
        if vid and vid not in ids:
            ids.append(vid)
    title = _text(data.get("title"), 150) or c.title
    text = _text(data.get("text"), MAX_TEXT)
    return Plan(text if len(text) >= 15 else "",
                music.Pick(c.artist, title, tuple(ids[:3]), music.spotify_track_id(data.get("spotify"))))


def render(plan: Plan | None, c: Candidate, day: date, card: music.Card) -> tuple[str, str]:
    """(Telegram HTML, plain text) of the post: the song, why, its growth, then its card."""
    head = prompts.MUSIC_VIRAL_HEADER.format(date=f"{day.day} {MONTHS[day.month - 1]}")
    song = html.escape(c.title)
    if c.url:
        song = f'<a href="{html.escape(c.url, quote=True)}">{song}</a>'
    line = f"<b>{html.escape(c.artist)}</b> — «{song}»." + (f" {html.escape(plan.text)}" if plan and plan.text else "")
    growth = prompts.MUSIC_VIRAL_GROWTH.format(facts="; ".join(c.facts))
    body = "\n\n".join([head, line, html.escape(growth), card.html])
    plain = "\n\n".join([re.sub(r"<[^>]+>", "", head),
                         f"{c.artist} — «{c.title}»." + (f" {plan.text}" if plan and plan.text else "")
                         + (f" {c.url}" if c.url else ""), growth, card.plain])
    return body, plain
