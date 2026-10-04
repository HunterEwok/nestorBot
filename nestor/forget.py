"""/forget_me: a person's words leave the archive, with every copy of them the archive keeps.

Used by the answerer (the command itself, and answers logged while it ran) and by the restore
from a backup, which replays every opt-out. The caller holds the transaction.

Every transaction that erases (this module, the nightly purge in nestor.retention) takes
ERASE_LOCK before it locks any row, so two of them never wait for each other's rows.
"""
from __future__ import annotations

import asyncpg

ERASE_LOCK = 0x6E6573746F73      # nestor.migrate takes ...72; transaction-scoped, released at commit


async def erase_lock(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock($1)", ERASE_LOCK)


async def forget(conn: asyncpg.Connection, user_id: int, bot_id: int) -> int:
    """Delete the user's messages and reminders everywhere, the copies of their words in other
    people's replies, forwards and the bot's answers, and their name; keep only the opt-out, so
    nothing of theirs is stored again. Returns the number of messages deleted."""
    await erase_lock(conn)
    await conn.execute(
        """INSERT INTO users (user_id, opted_out) VALUES ($1, TRUE)
           ON CONFLICT (user_id) DO UPDATE
             SET opted_out = TRUE, username = NULL, first_name = NULL, last_name = NULL, updated_at = now()""",
        user_id)
    # Quotes of their words in replies, found by the replied message: imported replies do not say
    # who wrote the message they reply to.
    await conn.execute(
        """UPDATE messages r SET raw = r.raw - 'quote'
           FROM messages m
           WHERE m.user_id = $1 AND r.chat_id = m.chat_id AND r.reply_to_id = m.message_id AND r.raw ? 'quote'""",
        user_id)
    # ... and by the author Telegram names, for replies to messages the archive does not have.
    await conn.execute(
        """UPDATE messages SET raw = raw - 'quote'
           WHERE raw ? 'quote' AND raw->'reply_to_message'->'from'->>'id' = $1::bigint::text""", user_id)
    # Their messages forwarded by others: the row is the forwarder's, the words are theirs.
    await conn.execute(
        """UPDATE messages
           SET text = NULL, entities = NULL,
               forward_origin = jsonb_build_object('type', forward_origin->'type',
                                                   'sender_user', jsonb_build_object('id', $1::bigint)),
               raw = (raw - 'text' - 'caption' - 'entities' - 'caption_entities')
                     || jsonb_build_object('forward_origin', jsonb_build_object(
                          'type', forward_origin->'type', 'sender_user', jsonb_build_object('id', $1::bigint)))
           WHERE forward_origin->'sender_user'->>'id' = $1::bigint::text""", user_id)
    status = await conn.execute("DELETE FROM messages WHERE user_id = $1", user_id)
    await conn.execute("UPDATE bot_requests SET question = NULL WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM reminders WHERE user_id = $1", user_id)
    await scrub_answers(conn, user_id, bot_id)
    return int(status.split()[-1])


async def scrub_answers(conn: asyncpg.Connection, user_id: int, bot_id: int, request_id: int | None = None) -> None:
    """The bot's answers that carried the user's words (quote-links and the rest that
    bot_requests.quoted_user_ids records): out of the archive, and out of the conversation
    history that goes back to the model. All of them, or one request's."""
    for r in await conn.fetch(
            """SELECT chat_id, answer_msg_ids FROM bot_requests
               WHERE $1 = ANY(quoted_user_ids) AND answer_msg_ids IS NOT NULL
                 AND ($2::bigint IS NULL OR id = $2)""", user_id, request_id):
        await conn.execute("DELETE FROM messages WHERE chat_id = $1 AND message_id = ANY($2::bigint[]) "
                           "AND user_id = $3", r["chat_id"], r["answer_msg_ids"], bot_id)
        await conn.execute(
            """UPDATE messages SET raw = raw - 'quote'
               WHERE chat_id = $1 AND raw ? 'quote'
                 AND raw->'reply_to_message'->>'message_id' = ANY($2::text[])""",
            r["chat_id"], [str(i) for i in r["answer_msg_ids"]])
    await conn.execute("UPDATE bot_requests SET answer = NULL WHERE $1 = ANY(quoted_user_ids) "
                       "AND ($2::bigint IS NULL OR id = $2)", user_id, request_id)


async def scrub_if_opted_out(conn: asyncpg.Connection, request_id: int, quoted: list[int] | None,
                             bot_id: int) -> None:
    """A /forget_me that came while the answer was being written: scrub the row at once, in the
    transaction that logged it. The lock waits for a /forget_me in flight to finish, so neither
    side misses the other."""
    if not quoted:
        return
    await erase_lock(conn)
    rows = await conn.fetch("SELECT user_id, opted_out FROM users WHERE user_id = ANY($1::bigint[]) FOR SHARE",
                            quoted)
    for r in rows:
        if r["opted_out"]:
            await scrub_answers(conn, r["user_id"], bot_id, request_id)
