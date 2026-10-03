"""/export: the asker's own archived messages as a plain text file, sent to a private chat."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg

from .context import message_link


async def fetch(conn: asyncpg.Connection, user_id: int) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        """SELECT m.chat_id, c.title, m.message_id, m.thread_id, m.is_topic_message, m.sent_at, m.kind,
                  m.text, m.edited_at
           FROM messages m LEFT JOIN chats c ON c.chat_id = m.chat_id
           WHERE m.user_id = $1 AND m.kind <> 'service'
           ORDER BY c.title NULLS LAST, m.chat_id, m.sent_at, m.message_id""", user_id))


def render(rows, user: dict, tz: ZoneInfo, now: datetime) -> bytes:
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or "без имени"
    if user.get("username"):
        name += f" (@{user['username']})"
    out = ["Нестор: твои сообщения из архива", f"{name}, id {user['id']}",
           f"Выгрузка от {now.astimezone(tz):%Y-%m-%d %H:%M} ({tz.key}), сообщений: {len(rows)}"]
    chat = None
    for r in rows:
        if r["chat_id"] != chat:
            chat = r["chat_id"]
            out += ["", f"=== {r['title'] or chat} ===", ""]
        when = r["sent_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M")
        edited = " (изм.)" if r["edited_at"] else ""
        link = message_link(r["chat_id"], r["message_id"], r["thread_id"], r["is_topic_message"]) or ""
        out.append(f"{when}{edited} {link}".rstrip())
        body = r["text"] or ""
        if r["kind"] != "text":
            body = f"[{r['kind']}] {body}".rstrip()
        out += ["  " + line for line in (body.splitlines() or [""])]
    return ("\n".join(out) + "\n").encode("utf-8")


def filename(now: datetime, tz: ZoneInfo) -> str:
    return f"nestor-export-{now.astimezone(tz):%Y-%m-%d}.txt"
