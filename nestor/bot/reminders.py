"""Reminders: a deterministic parser of Russian time expressions and the reminders table.

«напомни в пятницу про сбор денег», «напомни через 2 часа позвонить маме»,
«напомни 10.10 в 18:30 оплатить квартиру». No model is involved: the time is parsed
here and the bot repeats it back, so a wrong guess is visible at once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from . import prompts

DEFAULT_TIME = time(10, 0)          # «напомни завтра» without a time
MAX_AHEAD = timedelta(days=366)     # the archive keeps 12 months, so do reminders
MIN_AHEAD = timedelta(seconds=30)

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")
_WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "четверг": 3, "пятницу": 4, "субботу": 5, "воскресенье": 6}
_WEEKDAYS_ACC = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")
_PARTS_OF_DAY = {"утром": time(9), "днём": time(13), "днем": time(13), "вечером": time(19), "ночью": time(23)}
_WORD_NUMBERS = {"пару": 2, "несколько": 3, "полтора": 1.5, "полторы": 1.5, "один": 1, "одну": 1, "два": 2,
                 "две": 2, "три": 3, "четыре": 4, "пять": 5, "десять": 10, "пятнадцать": 15, "двадцать": 20,
                 "тридцать": 30, "сорок": 40}

_NUM = r"\d+(?:[.,]\d+)?|" + "|".join(_WORD_NUMBERS)
_RELATIVE = re.compile(
    rf"(?<!\w)через\s+(?:(?P<n>{_NUM})\s+)?(?P<unit>минут[уы]?|мин|час(?:а|ов)?|ч|дн(?:я|ей)|день|сутки|суток|"
    r"недел[юиь]|недель|месяц(?:а|ев)?|год|года|лет)(?!\w)\.?"
    r"|(?<!\w)через\s+(?P<half>полчаса)(?!\w)", re.I)
# 18:30 anywhere; «в 18.30», «в 10», «к 9 вечера», «в 3 часа дня»; «в полдень».
_TIME = re.compile(
    r"(?:(?<!\w)(?:в|к|на)\s+)?(?<![\d.:])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?!\d)(?:\s*(?P<p1>утра|дня|вечера|ночи)(?!\w))?"
    r"|(?<!\w)(?:в|к)\s+(?P<h2>[01]?\d|2[0-3])\.(?P<m2>[0-5]\d)(?!\d)(?:\s*(?P<p2>утра|дня|вечера|ночи)(?!\w))?"
    r"|(?<!\w)(?:в|к)\s+(?P<h3>[01]?\d|2[0-3])(?![\d.:])(?:\s*(?:часа|часов|час|ч)(?!\w))?(?:\s*(?P<p3>утра|дня|вечера|ночи)(?!\w))?"
    r"(?!\s*(?:раз|руб|рубл|доллар|евро|процент|%|человек|минут|мин|сек|дн|день|дня|недел|месяц|лет|год|числ|штук))"
    r"|(?<!\w)в\s+(?P<noon>полдень|полночь)(?!\w)", re.I)
_DATE = re.compile(
    r"(?<![\d.:])(?P<d>[0-3]?\d)\.(?P<mo>[01]?\d)(?:\.(?P<y>\d{4}|\d{2}))?(?![\d:])"
    rf"|(?<!\w)(?P<d2>[0-3]?\d)(?:-?го)?\s+(?P<mon>{'|'.join(MONTHS)})(?:\s+(?P<y2>\d{{4}})(?:\s*(?:года|г\.?))?)?(?!\w)"
    r"|(?<!\w)(?P<d3>[0-3]?\d)(?:-?го)?\s+числа(?!\w)", re.I)
_WEEKDAY = re.compile(
    r"(?<!\w)во?\s+(?:(?:эту|этот|это|следующую|следующий|следующее|ближайшую|ближайший|ближайшее)\s+)?"
    rf"(?P<wd>{'|'.join(_WEEKDAYS)})(?!\w)", re.I)
_DAY_WORD = re.compile(r"(?<!\w)(?P<dw>послезавтра|завтра|сегодня)(?!\w)", re.I)
_PART = re.compile(rf"(?<!\w)(?P<part>{'|'.join(_PARTS_OF_DAY)})(?!\w)", re.I)

_START = re.compile(r"^\s*(?:пожалуйста\s*,?\s*)?(?:напомни(?:те)?|поставь\s+напоминание|создай\s+напоминание)(?!\w)"
                    r"[\s,:]*(?:пожалуйста\s*,?\s*)?(?:(?:мне|нам|всем|ему|ей|им)(?!\w)\s*)?", re.I)
_LEAD = re.compile(r"^(?:о\s+том\s*,?\s*что|о\s+том\s*,?\s*чтобы|что(?:бы)?)(?!\w)\s*", re.I)


# «напомни, о чём говорили вчера», «напомни, кто это сказал?» are questions about the chat.
_QUESTION = re.compile(r"^(?:о\s+ч[её]м|кто|где|как|какой|какая|какое|какие|когда|сколько|зачем|почему|"
                       r"что\s+(?:мы|вы|он|она|они|я|ты|там|тут|здесь)(?!\w))(?!\w)", re.I)


def is_request(text: str) -> bool:
    """«напомни …» with a time in it, and not a question about the chat."""
    m = _START.match(text or "")
    if not m or text.rstrip().endswith("?") or _QUESTION.match(text[m.end():]):
        return False
    return has_time(text)


def has_time(text: str) -> bool:
    return any(p.search(text or "") for p in (_RELATIVE, _TIME, _DATE, _WEEKDAY, _DAY_WORD, _PART))


class ReminderError(ValueError):
    """The text names a time that cannot be used; the message is for the asker."""


@dataclass(frozen=True)
class Parsed:
    due: datetime   # aware
    text: str       # what to remind about, the asker's words without the time


def _hour(h: int, period: str | None) -> int:
    period = (period or "").lower()
    if period in ("дня", "вечера") and h < 12:
        return h + 12 if period == "вечера" or h <= 6 else h
    if period == "ночи":
        return 0 if h == 12 else (h + 12 if 9 <= h <= 11 else h)
    if period == "утра" and h == 12:
        return 0
    return h


def _number(raw: str | None) -> float:
    if not raw:
        return 1
    raw = raw.lower()
    return _WORD_NUMBERS.get(raw) or float(raw.replace(",", "."))


def _cut(text: str, m: re.Match) -> str:
    return text[:m.start()] + " " + text[m.end():]


def parse(text: str, now: datetime, tz: ZoneInfo) -> Parsed | None:
    """None if the text names no time at all. Raises ReminderError for past or too distant times."""
    local_now = now.astimezone(tz)
    rest = _START.sub("", text or "", count=1)
    due: datetime | None = None

    m = _RELATIVE.search(rest)
    if m:
        rest = _cut(rest, m)
        if m.group("half"):
            delta = timedelta(minutes=30)
        else:
            n, unit = _number(m.group("n")), m.group("unit").lower()
            if unit.startswith("мин"):
                delta = timedelta(minutes=n)
            elif unit.startswith("ч"):
                delta = timedelta(hours=n)
            elif unit.startswith(("д", "сут")):
                delta = timedelta(days=n)
            elif unit.startswith("недел"):
                delta = timedelta(weeks=n)
            elif unit.startswith("месяц"):
                delta = timedelta(days=30 * n)
            else:
                delta = timedelta(days=365 * n)
        due = now + delta
    else:
        at: time | None = None
        m = _TIME.search(rest)
        if m:
            rest = _cut(rest, m)
            if m.group("noon"):
                at = time(12) if m.group("noon").lower() == "полдень" else time(0)
            else:
                h = m.group("h") or m.group("h2") or m.group("h3")
                minute = m.group("m") or m.group("m2") or "0"
                at = time(_hour(int(h), m.group("p1") or m.group("p2") or m.group("p3")), int(minute))

        day: date | None = None
        explicit_year = False
        m = _DATE.search(rest)
        if m:
            rest = _cut(rest, m)
            if m.group("d3"):
                d = int(m.group("d3"))
                day = _day_of_month(local_now.date(), d)
            else:
                d = int(m.group("d") or m.group("d2"))
                mo = int(m.group("mo")) if m.group("mo") else MONTHS.index(m.group("mon").lower()) + 1
                y = m.group("y") or m.group("y2")
                explicit_year = y is not None
                year = (2000 + int(y) if len(y) == 2 else int(y)) if y else local_now.year
                try:
                    day = date(year, mo, d)
                except ValueError:
                    raise ReminderError(prompts.REMINDER_BAD_DATE) from None
                if not explicit_year and day < local_now.date():
                    day = date(year + 1, mo, d)
        else:
            m = _WEEKDAY.search(rest)
            if m:
                rest = _cut(rest, m)
                ahead = (_WEEKDAYS[m.group("wd").lower()] - local_now.weekday()) % 7
                day = local_now.date() + timedelta(days=ahead)
                if ahead == 0 and (at is None or datetime.combine(day, at, tz) <= local_now):
                    day += timedelta(days=7)
            else:
                m = _DAY_WORD.search(rest)
                if m:
                    rest = _cut(rest, m)
                    day = local_now.date() + timedelta(days=("сегодня", "завтра", "послезавтра").index(m.group("dw").lower()))

        m = _PART.search(rest) if at is None else None
        if m:
            rest = _cut(rest, m)
            at = _PARTS_OF_DAY[m.group("part").lower()]
        if day is None and at is None:
            return None
        if day is None:  # only a time: today, or tomorrow if it has passed
            day = local_now.date()
            if datetime.combine(day, at, tz) <= local_now:
                day += timedelta(days=1)
        due = datetime.combine(day, at or DEFAULT_TIME, tz)

    if due - now < MIN_AHEAD:
        raise ReminderError(prompts.REMINDER_PAST)
    if due - now > MAX_AHEAD:
        raise ReminderError(prompts.REMINDER_TOO_FAR)
    return Parsed(due, _clean(rest))


def _day_of_month(today: date, d: int) -> date:
    """«10 числа»: this month if still ahead, else the next month that has that day."""
    year, month = today.year, today.month
    for _ in range(13):
        try:
            candidate = date(year, month, d)
        except ValueError:
            candidate = None
        if candidate and candidate >= today:
            return candidate
        month += 1
        if month > 12:
            year, month = year + 1, 1
    raise ReminderError(prompts.REMINDER_BAD_DATE)


def _clean(rest: str) -> str:
    text = re.sub(r"\s+", " ", rest).strip(" ,.;:—-!")
    text = re.sub(r"^(?:пожалуйста|плиз)(?!\w)[\s,]*", "", text, flags=re.I)
    text = re.sub(r"[\s,]*(?:пожалуйста|плиз)$", "", text, flags=re.I)
    text = _LEAD.sub("", text).strip(" ,.;:—-")
    return text[:500]


def about(text: str) -> str:
    """«про сбор денег» reads as is; anything else after a colon."""
    if not text:
        return ""
    if re.match(r"^(?:про|о|об|обо)(?!\w)", text, re.I):
        return " " + text
    return ": " + text


def format_due(due: datetime, tz: ZoneInfo, now: datetime) -> str:
    local, today = due.astimezone(tz), now.astimezone(tz).date()
    hm = local.strftime("%H:%M")
    if local.date() == today:
        return f"сегодня в {hm}"
    if local.date() == today + timedelta(days=1):
        return f"завтра в {hm}"
    year = f" {local.year}" if local.year != today.year else ""
    return f"{_WEEKDAYS_ACC[local.weekday()]}, {local.day} {MONTHS[local.month - 1]}{year}, в {hm}"


# ---- table ----

async def create(conn: asyncpg.Connection, *, chat_id: int, thread_id: int | None, user_id: int,
                 request_msg_id: int, text: str, due: datetime) -> int:
    return await conn.fetchval(
        """INSERT INTO reminders (chat_id, thread_id, user_id, request_msg_id, text, due_at)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        chat_id, thread_id, user_id, request_msg_id, text, due)


async def count_pending(conn, chat_id: int, user_id: int) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM reminders WHERE chat_id = $1 AND user_id = $2 AND sent_at IS NULL "
        "AND cancelled_at IS NULL", chat_id, user_id)


async def pending_for(conn, chat_id: int, user_id: int) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        "SELECT id, text, due_at FROM reminders WHERE chat_id = $1 AND user_id = $2 AND sent_at IS NULL "
        "AND cancelled_at IS NULL ORDER BY due_at, id", chat_id, user_id))


async def cancel(conn, reminder_id: int, chat_id: int, user_id: int | None) -> bool:
    """user_id None: any reminder of the chat (group admins)."""
    status = await conn.execute(
        """UPDATE reminders SET cancelled_at = now()
           WHERE id = $1 AND chat_id = $2 AND ($3::bigint IS NULL OR user_id = $3)
             AND sent_at IS NULL AND cancelled_at IS NULL""", reminder_id, chat_id, user_id)
    return status.endswith(" 1")


async def due(conn, now: datetime, limit: int = 20) -> list[asyncpg.Record]:
    """Due reminders of chats that are not paused, with the asker's name for the mention."""
    return list(await conn.fetch(
        """SELECT r.id, r.chat_id, r.thread_id, r.user_id, r.request_msg_id, r.text, r.due_at, r.attempts,
                  u.first_name, u.last_name, u.username
           FROM reminders r
           JOIN chats c ON c.chat_id = r.chat_id AND c.enabled
           LEFT JOIN users u ON u.user_id = r.user_id
           WHERE r.sent_at IS NULL AND r.cancelled_at IS NULL AND r.due_at <= $1
           ORDER BY r.due_at, r.id LIMIT $2""", now, limit))


async def mark_sent(conn, reminder_id: int) -> None:
    await conn.execute("UPDATE reminders SET sent_at = now(), attempts = attempts + 1 WHERE id = $1", reminder_id)


async def mark_failed(conn, reminder_id: int, *, give_up: bool) -> None:
    await conn.execute(
        "UPDATE reminders SET attempts = attempts + 1, cancelled_at = CASE WHEN $2 THEN now() END WHERE id = $1",
        reminder_id, give_up)
