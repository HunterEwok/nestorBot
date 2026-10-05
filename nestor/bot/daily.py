"""Scheduled posts: the group digest and the wisdom of the day.

The digest comes at DIGEST_TIME once every N days (DIGEST_EVERY_DAYS, or the group's own
/digest N) and retells the N local days before that morning; with N = 1 it is the morning
digest of yesterday.

The wisdom is searched by a model with internet access, which must never see the chat.
So the archive model only picks today's themes from the fixed list below, the list is
checked here, and only those words reach the web profile: no chat text can end up in a
search query.
"""
from __future__ import annotations

import html
import ipaddress
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import asyncpg

from . import prompts
from .context import plural
from .llm import extract_json
from .reminders import MONTHS

DIGEST_MAX_DAYS = 90        # DIGEST_EVERY_DAYS and /digest N: from 1 to this
DAYS = ("день", "дня", "дней")

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
    """The post in Telegram HTML: the quote as a quote block, the source as a link that shows its domain."""
    host = (urlparse(w.source_url).hostname or "").removeprefix("www.")
    lines = [f"<b>{html.escape(prompts.WISDOM_HEADER)}</b>", "",
             f"<blockquote>«{html.escape(w.quote)}»\n— <i>{html.escape(w.author)}</i></blockquote>"]
    if w.theme:
        lines.append(html.escape(prompts.WISDOM_THEME.format(theme=w.theme)))
    lines.append(f'🔗 <a href="{html.escape(w.source_url, quote=True)}">{html.escape(prompts.WISDOM_SOURCE)}</a>'
                 f" ({html.escape(host)})")
    return "\n".join(lines)


def day_name(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


def days_name(n: int) -> str:
    """5 -> «5 дней», 21 -> «21 день»."""
    return f"{n} {plural(n, DAYS)}"


def digest_bounds(day: date, days: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """What the digest posted on `day` retells: [day - days, day), local midnight to local midnight,
    so a day with a clock change is as long as it really was."""
    return datetime.combine(day - timedelta(days=days), time(0), tz), datetime.combine(day, time(0), tz)


def span_name(first: date, last: date) -> str:
    """«5 сентября — 4 октября», or one day's name."""
    return day_name(first) if first == last else f"{day_name(first)} — {day_name(last)}"


def digest_period(day: date, days: int) -> str:
    """How the digest posted on `day` names what it retells: «4 октября» for yesterday,
    «30 дней, 5 сентября — 4 октября» for more."""
    if days == 1:
        return day_name(day - timedelta(days=1))
    return f"{days_name(days)}, {span_name(day - timedelta(days=days), day - timedelta(days=1))}"


def digest_header(day: date, days: int) -> str:
    """The first line of the post (Markdown, as the model's text under it)."""
    if days == 1:
        return prompts.DIGEST_HEADER.format(date=digest_period(day, 1))
    return prompts.DIGEST_PERIOD_HEADER.format(period=digest_period(day, days))


# ---- the daily_posts table: at most one post per chat, kind and local day ----

async def claim(conn: asyncpg.Connection, chat_id: int, kind: str, day: date) -> int | None:
    """Row id if this call may make the post now: first time today, or a retry of a failed
    attempt (after 10 minutes, at most 3 attempts) or of one that died mid-way (after 30).
    A post still waiting for material (the news before the sources are read) is tried again
    every 10 minutes, at most 6 attempts."""
    return await conn.fetchval(
        """INSERT INTO daily_posts (chat_id, kind, day, status) VALUES ($1, $2, $3, 'running')
           ON CONFLICT (chat_id, kind, day) DO UPDATE
             SET status = 'running', attempts = daily_posts.attempts + 1, updated_at = now()
             WHERE (daily_posts.attempts < 3
                    AND ((daily_posts.status = 'failed' AND daily_posts.updated_at < now() - interval '10 minutes')
                      OR (daily_posts.status = 'running' AND daily_posts.updated_at < now() - interval '30 minutes')))
                OR (daily_posts.attempts < 6 AND daily_posts.status = 'waiting'
                    AND daily_posts.updated_at < now() - interval '10 minutes')
           RETURNING id""", chat_id, kind, day)


async def digest_due(conn, chat_id: int, day: date, every: int) -> bool:
    """The digest is due on `day` when the last one posted here is at least `every` days back, or
    there was none yet. Skipped and failed days do not count: the next day tries again."""
    last = await conn.fetchval(
        "SELECT max(day) FROM daily_posts WHERE chat_id = $1 AND kind = 'digest' AND status = 'posted'", chat_id)
    return last is None or (day - last).days >= every


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
