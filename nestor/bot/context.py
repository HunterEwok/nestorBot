"""Context the collector prepares for the model: always scoped to one chat (and its
pre-migration predecessor), rendered as clearly marked data."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from . import reminders

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
                          limit: int = 400, *, until: datetime | None = None,
                          humans_only: bool = False) -> list[asyncpg.Record]:
    """The latest `limit` messages of [since, until), oldest first. humans_only: without bots."""
    rows = await conn.fetch(
        f"""SELECT {_COLUMNS} {_FROM}
            WHERE m.chat_id = ANY($1::bigint[]) AND {_VISIBLE} AND m.sent_at >= $3
              AND ($5::timestamptz IS NULL OR m.sent_at < $5)
              AND (NOT $6::bool OR u.user_id IS NULL OR NOT u.is_bot)
              AND ($2::bigint IS NULL OR (m.thread_id = $2 AND m.is_topic_message))
            ORDER BY m.sent_at DESC LIMIT $4""",
        chat_ids, thread_id, since, limit, until, humans_only,
    )
    return rows[::-1]


async def last_message_at(conn, chat_ids: list[int], thread_id: int | None, user_id: int, *,
                          before: datetime, exclude_id: int) -> datetime | None:
    """When the user last wrote here before the current message (for «что я пропустил»)."""
    return await conn.fetchval(
        """SELECT max(sent_at) FROM messages
           WHERE chat_id = ANY($1::bigint[]) AND user_id = $3 AND sent_at <= $4 AND message_id <> $5
             AND ($2::bigint IS NULL OR (thread_id = $2 AND is_topic_message))""",
        chat_ids, thread_id, user_id, before, exclude_id)


async def count_since(conn, chat_ids: list[int], thread_id: int | None, since: datetime, *,
                      except_user: int) -> int:
    """Visible messages by others (bots excluded) since the moment."""
    return await conn.fetchval(
        f"""SELECT count(*) {_FROM}
            WHERE m.chat_id = ANY($1::bigint[]) AND {_VISIBLE} AND m.sent_at > $3
              AND m.user_id IS DISTINCT FROM $4 AND (u.user_id IS NULL OR NOT u.is_bot)
              AND ($2::bigint IS NULL OR (m.thread_id = $2 AND m.is_topic_message))""",
        chat_ids, thread_id, since, except_user)


async def addressed_to(conn, chat_ids: list[int], thread_id: int | None, user_id: int, username: str | None,
                       since: datetime, limit: int = 50) -> list[asyncpg.Record]:
    """Replies to the user's messages and @mentions of the user since the moment."""
    rows = await conn.fetch(
        f"""SELECT {_COLUMNS} {_FROM}
            WHERE m.chat_id = ANY($1::bigint[]) AND {_VISIBLE} AND m.sent_at > $3
              AND m.user_id IS DISTINCT FROM $4 AND (u.user_id IS NULL OR NOT u.is_bot)
              AND ($2::bigint IS NULL OR (m.thread_id = $2 AND m.is_topic_message))
              AND (EXISTS (SELECT 1 FROM messages p
                           WHERE p.chat_id = m.chat_id AND p.message_id = m.reply_to_id AND p.user_id = $4)
                   OR ($5::text IS NOT NULL AND m.text ILIKE '%@' || $5 || '%'))
            ORDER BY m.sent_at DESC LIMIT $6""",
        chat_ids, thread_id, since, user_id, username, limit,
    )
    return rows[::-1]


async def top_branches(conn, chat_ids: list[int], thread_id: int | None, since: datetime,
                       limit: int = 5, min_size: int = 3) -> list[asyncpg.Record]:
    """The largest reply trees among messages since the moment: chat_id, root, size and the
    ids of the period's messages in the tree. Bots and people who opted out are not counted."""
    return list(await conn.fetch(
        """WITH RECURSIVE period AS (
               SELECT m.chat_id, m.message_id, m.reply_to_id
               FROM messages m LEFT JOIN users u ON u.user_id = m.user_id
               WHERE m.chat_id = ANY($1::bigint[]) AND m.sent_at >= $3 AND m.kind <> 'service'
                 AND (u.user_id IS NULL OR NOT (u.opted_out OR u.is_bot))
                 AND ($2::bigint IS NULL OR (m.thread_id = $2 AND m.is_topic_message))),
           up(chat_id, leaf, node, parent, depth) AS (
               SELECT chat_id, message_id, message_id, reply_to_id, 0 FROM period
             UNION ALL
               SELECT up.chat_id, up.leaf, m.message_id, m.reply_to_id, up.depth + 1
               FROM up JOIN messages m ON m.chat_id = up.chat_id AND m.message_id = up.parent
               WHERE up.depth < 200),
           roots AS (
               SELECT DISTINCT ON (chat_id, leaf) chat_id, leaf, node AS root
               FROM up ORDER BY chat_id, leaf, depth DESC)
           SELECT chat_id, root, count(*) AS size, array_agg(leaf ORDER BY leaf) AS members
           FROM roots GROUP BY chat_id, root HAVING count(*) >= $4
           ORDER BY size DESC, root DESC LIMIT $5""",
        chat_ids, thread_id, since, min_size, limit))


async def messages_by_id(conn, chat_id: int, message_ids: list[int]) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        f"""SELECT {_COLUMNS} {_FROM}
            WHERE m.chat_id = $1 AND m.message_id = ANY($2::bigint[]) AND {_VISIBLE}
            ORDER BY m.sent_at, m.message_id""", chat_id, message_ids))


async def typical_length(conn, chat_ids: list[int], user_id: int, limit: int = 30) -> int | None:
    """How long the person's recent messages here are, in characters (median); None if none."""
    return await conn.fetchval(
        """SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY length(text)) FROM (
               SELECT text FROM messages
               WHERE chat_id = ANY($1::bigint[]) AND user_id = $2 AND kind <> 'service'
                 AND coalesce(text, '') <> ''
               ORDER BY sent_at DESC LIMIT $3) recent""",
        chat_ids, user_id, limit)


def style_hint(typical: int | None, question: str) -> str:
    """One line for the prompt: how long the asker writes, so the answer can match it. Only
    a length, no chat text: it may go to the web profiles too."""
    size = max(typical or 0, len(question or ""))
    if size < 80:
        return "Стиль собеседника: пишет коротко. Ответь в 1–3 предложениях, если он не просит подробностей."
    if size < 250:
        return "Стиль собеседника: пишет средними сообщениями. Ответ — до 4–6 предложений."
    return "Стиль собеседника: пишет развёрнуто. Можно ответить подробнее, но без воды."


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(5, ("сообщение", "сообщения", "сообщений")) -> "сообщений"."""
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


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
_CATCHUP = re.compile(r"что\s+(?:я\s+)?пропустил|пока\s+меня\s+не\s+было|что\s+(?:тут|здесь)\s+было|catch\s*up", re.I)
# Only together with a reply to someone's message: that message is what gets checked.
_FACTCHECK = re.compile(r"провер|правда|верно|так\s+ли|факт|фейк|миф|вр[её]т|соврал|брехн|чушь|пруф|источник|"
                        r"подтверд|опроверг", re.I)
_BARE_FACTCHECK = re.compile(r"(?:проверь|проверка|это\s+правда|правда)\W*", re.I)
_LINKS = re.compile(r"ссылк|линк|(?<!\w)url|видос|видео|ролик|файл|документ|(?<!\w)pdf|пдф|картинк|фотк|фото|"
                    r"голосов|скидывал|кидал|присылал|постил", re.I)
_MUSIC = re.compile(r"^\s*(?:включи|вруби|поставь|сыграй|запусти|play)(?!\w)"
                    r"|^\s*(?:найди|скинь|пришли|дай)\s+(?:мне\s+|нам\s+)?(?:песн|трек|музык|клип|композици)", re.I)


def classify(question: str, *, reply_target: bool = False) -> str:
    """Scenario of a request. reply_target: it replies to someone's message (not the bot's)."""
    if reminders.is_request(question):
        return "reminder"
    if _MUSIC.search(question):
        return "music"
    if _CATCHUP.search(question):
        return "catchup"
    if (reply_target and _FACTCHECK.search(question)) or _BARE_FACTCHECK.fullmatch(question.strip()):
        return "factcheck"
    if _WEB.search(question) and not _CHAT.search(question):
        return "web"
    if _LINKS.search(question):
        return "links"
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
