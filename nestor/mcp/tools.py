"""Archive read tools. Every call needs the ticket from the prompt; it pins the call
to one chat (and its pre-migration predecessor), so injected text cannot reach other
groups. The role behind the connection can only SELECT from reader.* views."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import asyncpg

TZ = ZoneInfo("Europe/Tallinn")
MAX_LIMIT = 100


class ToolError(Exception):
    pass


async def _scope(conn: asyncpg.Connection, ticket: str) -> tuple[list[int], int | None]:
    row = await conn.fetchrow(
        """SELECT t.chat_id, t.thread_id, c.migrated_from_chat_id
           FROM reader.request_tickets t LEFT JOIN reader.chats c USING (chat_id)
           WHERE t.ticket = $1""", ticket)
    if row is None:
        raise ToolError("ticket недействителен или истёк: используй ticket из инструкции")
    ids = [row["chat_id"]] + ([row["migrated_from_chat_id"]] if row["migrated_from_chat_id"] else [])
    return ids, row["thread_id"]


def _limit(n: int | None, default: int) -> int:
    return max(1, min(int(n or default), MAX_LIMIT))


def render(rows) -> str:
    if not rows:
        return "Ничего не найдено."
    out = []
    for r in rows:
        when = r["sent_at"].astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        who = r["author_name"] or "без имени"
        if r["author_username"]:
            who += f" (@{r['author_username']})"
        reply = f" ↩#{r['reply_to_id']}" if r["reply_to_id"] else ""
        text = (r["text"] or "").replace("\n", " ⏎ ")
        if r["kind"] != "text":
            name = r.get("file_name")
            text = f"[{r['kind']}{': ' + name if name else ''}] {text}".rstrip()
        line = f"#{r['message_id']} {when} {who}{reply} {r['link'] or ''}\n  {text}"
        if r.get("urls"):
            line += "\n  ссылки: " + " ".join(r["urls"][:10])
        out.append(line)
    return "\n".join(out)


_SELECT = """SELECT message_id, sent_at, author_name, author_username, reply_to_id, kind, text, link
             FROM reader.messages"""
_IN_SCOPE = "chat_id = ANY($1::bigint[]) AND kind <> 'service'"


async def search_messages(conn, ticket: str, query: str, days: int = 365, author: str | None = None,
                          limit: int = 30) -> str:
    """Full-text search (Russian morphology) in this chat's archive."""
    ids, _ = await _scope(conn, ticket)
    rows = await conn.fetch(
        f"""{_SELECT} WHERE {_IN_SCOPE}
              AND tsv @@ websearch_to_tsquery('russian', $2)
              AND sent_at > now() - make_interval(days => $3)
              AND ($4::text IS NULL OR author_username ILIKE $4 OR author_name ILIKE '%' || $4 || '%')
            ORDER BY ts_rank(tsv, websearch_to_tsquery('russian', $2)) DESC, sent_at DESC LIMIT $5""",
        ids, query, int(days), (author or "").lstrip("@") or None, _limit(limit, 30))
    return render(sorted(rows, key=lambda r: r["sent_at"]))


async def get_thread(conn, ticket: str, message_id: int, depth: int = 10) -> str:
    """The message, what it replies to (up the chain) and the replies below it."""
    ids, _ = await _scope(conn, ticket)
    rows = await conn.fetch(
        f"""WITH RECURSIVE up(chat_id, message_id, d) AS (
                SELECT chat_id, message_id, 0 FROM reader.messages
                WHERE chat_id = ANY($1::bigint[]) AND message_id = $2
              UNION ALL
                SELECT m.chat_id, m.reply_to_id, up.d + 1 FROM up
                JOIN reader.messages m ON m.chat_id = up.chat_id AND m.message_id = up.message_id
                WHERE m.reply_to_id IS NOT NULL AND up.d < $3),
            down(chat_id, message_id, d) AS (
                SELECT chat_id, message_id, 0 FROM reader.messages
                WHERE chat_id = ANY($1::bigint[]) AND message_id = $2
              UNION ALL
                SELECT m.chat_id, m.message_id, down.d + 1 FROM down
                JOIN reader.messages m ON m.chat_id = down.chat_id AND m.reply_to_id = down.message_id
                WHERE down.d < $3)
            {_SELECT} m WHERE {_IN_SCOPE}
              AND (chat_id, message_id) IN (SELECT chat_id, message_id FROM up UNION SELECT chat_id, message_id FROM down)
            ORDER BY sent_at LIMIT 100""",
        ids, int(message_id), max(1, min(int(depth), 20)))
    return render(rows)


async def get_user_messages(conn, ticket: str, user: str, days: int = 1, limit: int = 30) -> str:
    """Messages of one participant (@username or name) in this chat."""
    ids, thread = await _scope(conn, ticket)
    name = user.lstrip("@")
    rows = await conn.fetch(
        f"""{_SELECT} WHERE {_IN_SCOPE}
              AND (author_username ILIKE $2 OR author_name ILIKE '%' || $2 || '%')
              AND sent_at > now() - make_interval(days => $3)
            ORDER BY sent_at DESC LIMIT $4""",
        ids, name, int(days), _limit(limit, 30))
    return render(rows[::-1])


async def get_recent(conn, ticket: str, limit: int = 50) -> str:
    """The latest messages of this chat (of the current forum topic, if any)."""
    ids, thread = await _scope(conn, ticket)
    rows = await conn.fetch(
        f"""SELECT message_id, sent_at, author_name, author_username, reply_to_id, kind, text, link
            FROM reader.messages WHERE {_IN_SCOPE}
              AND ($2::bigint IS NULL OR (thread_id = $2 AND is_topic_message))
            ORDER BY sent_at DESC LIMIT $3""",
        ids, thread, _limit(limit, 50))
    return render(rows[::-1])


_SELECT_SHARED = """SELECT message_id, sent_at, author_name, author_username, reply_to_id, kind, text, link,
                           urls, file_name
                    FROM reader.messages"""
FILE_KINDS = ("document", "photo", "video", "audio", "voice", "video_note", "animation")


async def find_links(conn, ticket: str, query: str | None = None, author: str | None = None, days: int = 365,
                     limit: int = 30) -> str:
    """Messages with links. `query` matches the link itself, the message text or its words."""
    ids, _ = await _scope(conn, ticket)
    rows = await conn.fetch(
        f"""{_SELECT_SHARED} WHERE {_IN_SCOPE} AND cardinality(urls) > 0
              AND sent_at > now() - make_interval(days => $2)
              AND ($3::text IS NULL OR author_username ILIKE $3 OR author_name ILIKE '%' || $3 || '%')
              AND ($4::text IS NULL OR text ILIKE '%' || $4 || '%'
                   OR array_to_string(urls, ' ') ILIKE '%' || $4 || '%'
                   OR tsv @@ websearch_to_tsquery('russian', $4))
            ORDER BY sent_at DESC LIMIT $5""",
        ids, int(days), (author or "").lstrip("@") or None, (query or "").strip() or None, _limit(limit, 30))
    return render(rows[::-1])


async def find_files(conn, ticket: str, query: str | None = None, kind: str | None = None,
                     author: str | None = None, days: int = 365, limit: int = 30) -> str:
    """Documents, photos, videos, audio, voice and video messages. `query` matches the file
    name or the caption."""
    ids, _ = await _scope(conn, ticket)
    kinds = [kind] if kind in FILE_KINDS else list(FILE_KINDS)
    rows = await conn.fetch(
        f"""{_SELECT_SHARED} WHERE {_IN_SCOPE} AND kind = ANY($2::text[])
              AND sent_at > now() - make_interval(days => $3)
              AND ($4::text IS NULL OR author_username ILIKE $4 OR author_name ILIKE '%' || $4 || '%')
              AND ($5::text IS NULL OR file_name ILIKE '%' || $5 || '%' OR text ILIKE '%' || $5 || '%'
                   OR tsv @@ websearch_to_tsquery('russian', $5))
            ORDER BY sent_at DESC LIMIT $6""",
        ids, kinds, int(days), (author or "").lstrip("@") or None, (query or "").strip() or None,
        _limit(limit, 30))
    return render(rows[::-1])
