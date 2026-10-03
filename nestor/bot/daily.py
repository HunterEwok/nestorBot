"""Scheduled posts: the morning digest and the wisdom of the day.

The wisdom is searched by a model with internet access, which must never see the chat.
So the archive model only picks today's themes from the fixed list below, the list is
checked here, and only those words reach the web profile: no chat text can end up in a
search query.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse

import asyncpg

from . import prompts
from .llm import extract_json
from .reminders import MONTHS

THEMES = (
    "любовь", "дружба", "семья", "дети", "родители", "одиночество", "счастье", "радость", "грусть", "страх",
    "смелость", "гнев", "терпение", "прощение", "благодарность", "доверие", "честность", "ложь", "справедливость",
    "свобода", "власть", "политика", "война", "мир", "история", "родина", "общество", "закон", "деньги",
    "богатство", "бедность", "работа", "лень", "успех", "неудача", "ошибки", "выбор", "ответственность",
    "привычки", "время", "перемены", "будущее", "прошлое", "возраст", "молодость", "старость", "здоровье",
    "спорт", "еда", "путешествия", "дом", "природа", "погода", "животные", "наука", "знание", "учёба", "книги",
    "искусство", "музыка", "кино", "игры", "юмор", "споры", "истина", "глупость", "мудрость", "технологии",
    "интернет", "машины", "космос", "вера", "жизнь", "смерть", "мечты", "надежда", "творчество", "красота",
    "слава", "зависть", "гордость", "щедрость", "бизнес", "экономика", "праздники", "отдых", "сон",
)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().replace("ё", "е"))


_THEME_KEYS = {_key(t): t for t in THEMES}


def parse_themes(answer: str) -> list[str]:
    """At most three themes, each exactly from THEMES; anything else is dropped."""
    data = extract_json(answer)
    items = data.get("themes") if isinstance(data, dict) else None
    themes: list[str] = []
    for item in items if isinstance(items, list) else []:
        theme = _THEME_KEYS.get(_key(item)) if isinstance(item, str) else None
        if theme and theme not in themes:
            themes.append(theme)
    return themes[:3]


@dataclass(frozen=True)
class Wisdom:
    quote: str
    author: str
    source_url: str
    theme: str | None


def is_public_url(url: Any) -> bool:
    """http(s) on a public host name: no IP literals, no localhost or home-network names."""
    if not isinstance(url, str) or len(url) > 500 or re.search(r"\s", url):
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower().rstrip(".")
    if u.scheme not in ("http", "https") or "." not in host or u.username or u.password:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not (host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".home", ".internal", ".arpa")))


def _field(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        return ""
    return value


def parse_wisdom(answer: str, themes: list[str]) -> Wisdom | None:
    data = extract_json(answer)
    if not isinstance(data, dict):
        return None
    quote = _field(data.get("quote"), 500).strip("\"«»“”„ ")
    author = _field(data.get("author"), 120).lstrip("—– -")
    url = data.get("source_url")
    if len(quote) < 10 or len(author) < 2 or not is_public_url(url):
        return None
    theme = _THEME_KEYS.get(_key(data["theme"])) if isinstance(data.get("theme"), str) else None
    return Wisdom(quote, author, url, theme if theme in themes else None)


def is_repeat(w: Wisdom, recent: list) -> bool:
    mine = _key(re.sub(r"[^\w\s]", " ", w.quote))
    for row in recent:
        old = _key(re.sub(r"[^\w\s]", " ", row["quote"] or ""))
        if old and (old == mine or (min(len(old), len(mine)) >= 30 and (old in mine or mine in old))):
            return True
    return False


def wisdom_request(themes: list[str], recent: list, day: date) -> str:
    lines = [f"Дата: {day.day} {MONTHS[day.month - 1]} {day.year}."]
    if themes:
        lines.append("Темы сегодняшних разговоров: " + ", ".join(themes) + ".")
    else:
        lines.append("Сегодня в чате почти не говорили: выбери тему сам.")
    if recent:
        lines.append("Недавние цитаты, их не повторять:")
        lines += [f"- {r['author']}: «{(r['quote'] or '')[:80]}»" for r in recent]
    return "\n".join(lines)


def format_wisdom(w: Wisdom) -> str:
    lines = [prompts.WISDOM_HEADER, "", f"«{w.quote}»", f"— {w.author}", ""]
    if w.theme:
        lines.append(prompts.WISDOM_THEME.format(theme=w.theme))
    lines.append(f"Источник: {w.source_url}")
    return "\n".join(lines)


def day_name(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


# ---- the daily_posts table: at most one post per chat, kind and local day ----

async def claim(conn: asyncpg.Connection, chat_id: int, kind: str, day: date) -> int | None:
    """Row id if this call may make the post now: first time today, or a retry of a failed
    attempt (after 10 minutes, at most 3 attempts) or of one that died mid-way (after 30)."""
    return await conn.fetchval(
        """INSERT INTO daily_posts (chat_id, kind, day, status) VALUES ($1, $2, $3, 'running')
           ON CONFLICT (chat_id, kind, day) DO UPDATE
             SET status = 'running', attempts = daily_posts.attempts + 1, updated_at = now()
             WHERE daily_posts.attempts < 3
               AND ((daily_posts.status = 'failed' AND daily_posts.updated_at < now() - interval '10 minutes')
                 OR (daily_posts.status = 'running' AND daily_posts.updated_at < now() - interval '30 minutes'))
           RETURNING id""", chat_id, kind, day)


async def finish(conn, post_id: int, status: str, *, message_id: int | None = None, themes: list[str] | None = None,
                 quote: str | None = None, author: str | None = None, source_url: str | None = None) -> None:
    await conn.execute(
        """UPDATE daily_posts SET status = $2, message_id = $3, themes = $4, quote = $5, author = $6,
                                  source_url = $7, updated_at = now()
           WHERE id = $1""", post_id, status, message_id, themes, quote, author, source_url)


async def recent_wisdom(conn, limit: int = 60) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        """SELECT quote, author FROM daily_posts WHERE kind = 'wisdom' AND status = 'posted' AND quote IS NOT NULL
           ORDER BY day DESC, id DESC LIMIT $1""", limit))
