"""Archive writes. Every function is idempotent: replaying an update changes nothing."""
from __future__ import annotations

import json
from enum import Enum

import asyncpg

from .parse import ChatRow, MessageRow, UserRow


class Result(str, Enum):
    INSERTED = "inserted"
    EDITED = "edited"
    DUPLICATE = "duplicate"
    PAUSED = "paused"          # chat disabled with /pause
    OPTED_OUT = "opted_out"    # author used /forget_me


async def init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str) -> asyncpg.Pool:
    # The database lives on another machine (over VPN or TLS): connections can die
    # silently, so a hung query must time out and land in the collector's retry loop,
    # and idle connections are recycled rather than trusted.
    return await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=4,
        init=init_connection,
        timeout=10,                              # connect
        command_timeout=30,                      # per query
        max_inactive_connection_lifetime=60,
    )


async def upsert_chat(conn: asyncpg.Connection, chat: ChatRow, migrated_from: int | None = None) -> bool:
    """Returns whether recording is enabled for the chat."""
    return await conn.fetchval(
        """INSERT INTO chats (chat_id, title, type, is_forum, migrated_from_chat_id)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (chat_id) DO UPDATE
             SET title = EXCLUDED.title, type = EXCLUDED.type, is_forum = EXCLUDED.is_forum,
                 migrated_from_chat_id = coalesce(chats.migrated_from_chat_id, EXCLUDED.migrated_from_chat_id)
           RETURNING enabled""",
        chat.chat_id, chat.title, chat.type, chat.is_forum, migrated_from,
    )


async def upsert_users(conn: asyncpg.Connection, users: list[UserRow]) -> None:
    for u in users:
        await conn.execute(
            """INSERT INTO users (user_id, username, first_name, last_name, is_bot)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id) DO UPDATE
                 SET username = EXCLUDED.username, first_name = EXCLUDED.first_name,
                     last_name = EXCLUDED.last_name, is_bot = EXCLUDED.is_bot, updated_at = now()
                 WHERE (users.username, users.first_name, users.last_name, users.is_bot)
                       IS DISTINCT FROM
                       (EXCLUDED.username, EXCLUDED.first_name, EXCLUDED.last_name, EXCLUDED.is_bot)""",
            u.user_id, u.username, u.first_name, u.last_name, u.is_bot,
        )


_INSERT_MESSAGE = """
INSERT INTO messages (chat_id, message_id, thread_id, is_topic_message, user_id, sender_chat_id,
                      sent_at, reply_to_id, kind, event, text, entities, forward_origin,
                      media_group_id, file_id, file_unique_id, edited_at, raw)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
ON CONFLICT (chat_id, message_id) DO NOTHING
"""


def _message_args(m: MessageRow) -> tuple:
    return (
        m.chat_id, m.message_id, m.thread_id, m.is_topic_message, m.user_id, m.sender_chat_id,
        m.sent_at, m.reply_to_id, m.kind, m.event, m.text, m.entities, m.forward_origin,
        m.media_group_id, m.file_id, m.file_unique_id, m.edited_at, m.raw,
    )


async def save_message(
    conn: asyncpg.Connection, chat: ChatRow, msg: MessageRow, *, is_edit: bool = False
) -> Result:
    """Stores a new or edited message in one transaction (FR-1, FR-2, FR-5, FR-6)."""
    migrated_from = msg.raw.get("migrate_from_chat_id")
    async with conn.transaction():
        if not await upsert_chat(conn, chat, migrated_from):
            return Result.PAUSED
        await upsert_users(conn, msg.users)
        if msg.user_id is not None and await conn.fetchval(
            "SELECT opted_out FROM users WHERE user_id = $1", msg.user_id
        ):
            return Result.OPTED_OUT

        if msg.event == "migrate_to_chat_id":
            await conn.execute(
                "UPDATE chats SET migrated_to_chat_id = $2 WHERE chat_id = $1",
                msg.chat_id, msg.raw["migrate_to_chat_id"],
            )

        if not is_edit:
            status = await conn.execute(_INSERT_MESSAGE, *_message_args(msg))
            return Result.INSERTED if status.endswith(" 1") else Result.DUPLICATE

        current = await conn.fetchrow(
            "SELECT text, edited_at FROM messages WHERE chat_id = $1 AND message_id = $2 FOR UPDATE",
            msg.chat_id, msg.message_id,
        )
        if current is None:
            # Edit of a message we never saw (sent before the bot joined, or lost):
            # keep what we have now.
            status = await conn.execute(_INSERT_MESSAGE, *_message_args(msg))
            return Result.INSERTED if status.endswith(" 1") else Result.DUPLICATE
        old_at, new_at = current["edited_at"], msg.edited_at
        if old_at is not None and new_at is not None and (
            old_at > new_at or (old_at == new_at and current["text"] == msg.text)
        ):
            return Result.DUPLICATE  # redelivery, or an older edit arriving late

        await conn.execute(
            """INSERT INTO message_edits (chat_id, message_id, old_text, old_edited_at, replaced_at)
               VALUES ($1, $2, $3, $4, coalesce($5, now()))""",
            msg.chat_id, msg.message_id, current["text"], current["edited_at"], msg.edited_at,
        )
        await conn.execute(
            """UPDATE messages
                  SET text = $3, entities = $4, edited_at = $5, raw = $6, kind = $7,
                      file_id = $8, file_unique_id = $9
                WHERE chat_id = $1 AND message_id = $2""",
            msg.chat_id, msg.message_id, msg.text, msg.entities, msg.edited_at, msg.raw,
            msg.kind, msg.file_id, msg.file_unique_id,
        )
        return Result.EDITED


async def allowed_by_migration(conn: asyncpg.Connection, chat_id: int, allowed: frozenset[int]) -> bool:
    """A supergroup created by migrating an allowed group is allowed too."""
    origin = await conn.fetchval("SELECT migrated_from_chat_id FROM chats WHERE chat_id = $1", chat_id)
    return origin is not None and origin in allowed
