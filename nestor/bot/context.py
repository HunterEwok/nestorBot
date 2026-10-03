"""Context the collector prepares for the model: always scoped to one chat (and its
pre-migration predecessor), rendered as clearly marked data."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg

_COLUMNS = """m.chat_id, m.message_id, m.thread_id, m.is_topic_message, m.user_id, m.sent_at,
              m.reply_to_id, m.kind, m.text, m.edited_at,
              u.first_name, u.last_name, u.username, u.is_bot"""
_FROM = "FROM messages m LEFT JOIN users u ON u.user_id = m.user_id"
_VISIBLE = "m.kind <> 'service' AND (u.user_id IS NULL OR NOT u.opted_out)"


def message_link(chat_id: int, message_id: int, thread_id: int | None = None, is_topic: bool = False) -> str | None:
    """t.me/c links exist only for supergroups (chat_id = -100<id>), SC-5."""
    if chat_id > -1000000000000:
        return None
    topic = f"{thread_id}/" if is_topic and thread_id else ""
    return f"https://t.me/c/{-chat_id - 1000000000000}/{topic}{message_id}"


async def chat_lineage(conn: asyncpg.Connection, chat_id: int) -> list[int]:
    """The chat plus the basic group it was migrated from, if any."""
    prev = await conn.fetchval("SELECT migrated_from_chat_id FROM chats WHERE chat_id = $1", chat_id)
    return [chat_id] if prev is None else [chat_id, prev]


async def recent(conn, chat_ids: list[int], thread_id: int | None, limit: int = 50) -> list[asyncpg.Record]:
    rows = await conn.fetch(
        f"""SELECT {_COLUMNS} {_FROM}
            WHERE m.chat_id = ANY($1::bigint[]) AND {_VISIBLE}
              AND ($2::bigint IS NULL OR (m.thread_id = $2 AND m.is_topic_message))
            ORDER BY m.sent_at DESC, m.message_id DESC LIMIT $3""",
        chat_ids, thread_id, limit,
    )
    return rows[::-1]


async def reply_chain(conn, chat_id: int, message_id: int, depth: int = 10) -> list[asyncpg.Record]:
    """The message and the messages it replies to, oldest first."""
    rows = await conn.fetch(
        f"""WITH RECURSIVE chain(message_id, depth) AS (
                SELECT $2::bigint, 0
              UNION ALL
                SELECT m.reply_to_id, c.depth + 1 FROM chain c
                JOIN messages m ON m.chat_id = $1 AND m.message_id = c.message_id
                WHERE m.reply_to_id IS NOT NULL AND c.depth < $3)
            SELECT {_COLUMNS} {_FROM} JOIN chain c ON m.chat_id = $1 AND m.message_id = c.message_id
            WHERE {_VISIBLE} ORDER BY m.sent_at, m.message_id""",
        chat_id, message_id, depth,
    )
    return list(rows)


async def replies_to(conn, chat_id: int, message_id: int, limit: int = 30) -> list[asyncpg.Record]:
    """Direct and nested replies below a message (the rest of its branch)."""
    return list(await conn.fetch(
        f"""WITH RECURSIVE branch(message_id, depth) AS (
                SELECT message_id, 1 FROM messages WHERE chat_id = $1 AND reply_to_id = $2
              UNION ALL
                SELECT m.message_id, b.depth + 1 FROM branch b
                JOIN messages m ON m.chat_id = $1 AND m.reply_to_id = b.message_id WHERE b.depth < 5)
            SELECT {_COLUMNS} {_FROM} JOIN branch b ON m.chat_id = $1 AND m.message_id = b.message_id
            WHERE {_VISIBLE} ORDER BY m.sent_at LIMIT $3""",
        chat_id, message_id, limit,
    ))


async def user_messages(conn, chat_ids: list[int], thread_id: int | None, user_id: int,
                        hours: int = 24, limit: int = 20) -> list[asyncpg.Record]:
    rows = await conn.fetch(
        f"""SELECT {_COLUMNS} {_FROM}
            WHERE m.chat_id = ANY($1::bigint[]) AND m.user_id = $3 AND {_VISIBLE}
              AND ($2::bigint IS NULL OR (m.thread_id = $2 AND m.is_topic_message))
              AND m.sent_at > now() - make_interval(hours => $4)
            ORDER BY m.sent_at DESC LIMIT $5""",
        chat_ids, thread_id, user_id, hours, limit,
    )
    return rows[::-1]


async def period_messages(conn, chat_ids: list[int], thread_id: int | None, since: datetime,
                          limit: int = 400) -> list[asyncpg.Record]:
    rows = await conn.fetch(
        f"""SELECT {_COLUMNS} {_FROM}
            WHERE m.chat_id = ANY($1::bigint[]) AND {_VISIBLE} AND m.sent_at >= $3
              AND ($2::bigint IS NULL OR (m.thread_id = $2 AND m.is_topic_message))
            ORDER BY m.sent_at DESC LIMIT $4""",
        chat_ids, thread_id, since, limit,
    )
    return rows[::-1]


async def mentioned_users(conn, msg: dict, bot_username: str) -> list[asyncpg.Record]:
    """Users named in the request by @username or by a text mention, except the bot."""
    text = msg.get("text") or msg.get("caption") or ""
    ids, names = [], []
    raw = text.encode("utf-16-le")
    for e in msg.get("entities") or msg.get("caption_entities") or []:
        if e.get("type") == "text_mention" and e.get("user"):
            ids.append(e["user"]["id"])
        elif e.get("type") == "mention":
            name = raw[e["offset"] * 2:(e["offset"] + e["length"]) * 2].decode("utf-16-le").lstrip("@")
            if name.lower() != bot_username.lower():
                names.append(name.lower())
    if not ids and not names:
        return []
    return list(await conn.fetch(
        """SELECT user_id, first_name, last_name, username FROM users
           WHERE NOT opted_out AND (user_id = ANY($1::bigint[]) OR lower(username) = ANY($2::text[]))""",
        ids, names,
    ))


def author(row) -> str:
    name = " ".join(p for p in (row["first_name"], row["last_name"]) if p) or "без имени"
    return f"{name} (@{row['username']})" if row["username"] else name


def render(rows, tz: ZoneInfo, *, title: str) -> str:
    """Messages as data lines: id, local time, author, reply target, link, text."""
    if not rows:
        return f"<{title}>\n(нет сообщений)\n</{title}>"
    lines = []
    for r in rows:
        when = r["sent_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M")
        reply = f" ↩#{r['reply_to_id']}" if r["reply_to_id"] else ""
        edited = " (изм.)" if r["edited_at"] else ""
        link = message_link(r["chat_id"], r["message_id"], r["thread_id"], r["is_topic_message"])
        text = (r["text"] or f"[{r['kind']}]").replace("\n", " ⏎ ")
        lines.append(f"#{r['message_id']} {when} {author(r)}{reply}{edited} {link or ''}\n  {text}")
    return f"<{title}>\n" + "\n".join(lines) + f"\n</{title}>"


_SCENARIOS = (
    ("who_is_right", re.compile(r"кто\s+(из\s+\w+\s+)?прав", re.I)),
    ("theses", re.compile(r"тезис|аргумент|оцени|разбери", re.I)),
    ("summary", re.compile(r"о\s*ч[её]м|кратко|итог|содержание|саммари|summary|что\s+обсуждали", re.I)),
    ("search", re.compile(r"кто\s+(и\s+когда\s+)?(говорил|писал)|найди|поиск|когда\s+(говорили|писали)", re.I)),
)


# Plainly about the outside world and not about this chat: goes straight to the web profile.
_WEB = re.compile(r"погод|прогноз|температур|курс\w*\s+(валют|доллар|евро|рубл|юан|биткоин|бакс)|новост|"
                  r"в\s+интернет|загугл|погугл|в\s+сети|правда\s+ли|сколько\s+стоит", re.I)
_CHAT = re.compile(r"чат|групп|переписк|говорил|писал|обсужда|сказал|здесь|тут\b|@\w", re.I)


def classify(question: str) -> str:
    if _WEB.search(question) and not _CHAT.search(question):
        return "web"
    for name, pattern in _SCENARIOS:
        if pattern.search(question):
            return name
    return "other"


_PERIODS = (
    (re.compile(r"недел", re.I), timedelta(days=7)),
    (re.compile(r"месяц", re.I), timedelta(days=30)),
    (re.compile(r"вчера", re.I), timedelta(days=2)),
    (re.compile(r"(\d+)\s*(ч|час)", re.I), None),
    (re.compile(r"(\d+)\s*(д|дн|день|дня|дней)", re.I), None),
)


def period_start(text: str, now: datetime, tz: ZoneInfo) -> datetime:
    """'за неделю', 'за 3 часа', 'сегодня' (default) -> start of the period."""
    for pattern, delta in _PERIODS:
        m = pattern.search(text or "")
        if m and delta is not None:
            return now - delta
        if m:
            n = int(m.group(1))
            return now - (timedelta(hours=n) if m.group(2).startswith("ч") else timedelta(days=n))
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)
