"""Import group history through the owner's Telegram account.

    python -m nestor.importer --list            # show your groups and their chat ids
    python -m nestor.importer                   # import every chat in ALLOWED_CHAT_IDS
    python -m nestor.importer --chat -100123    # import one chat

Needs TG_API_ID / TG_API_HASH (https://my.telegram.org → API development tools),
DATABASE_URL (collector role), BOT_ID (the number before the colon in the bot's token; the
token itself stays on the Pi) and ALLOWED_CHAT_IDS in the environment or .env.
The first run asks for your phone number and login code and stores the session in
nestor-import.session next to it; keep that file private, it is a logged-in account.
Re-running is safe: already stored messages are skipped, and an interrupted run
resumes after the last imported message. The bot's own messages are not imported: its
answers are kept in bot_requests with what they were built from, and an answer that
/forget_me or the nightly purge took out of the archive must not come back.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

import asyncpg
from dotenv import load_dotenv
from telethon import TelegramClient, functions, utils
from telethon.tl import types as t

from ..collector import store
from ..collector.parse import parse_chat, parse_message
from ..config import _parse_ids
from ..logs import setup_logging
from .convert import IMPORT_SOURCE, chat_dict, to_bot_api

log = logging.getLogger("nestor.importer")
BATCH = 200
PLACEHOLDER_BOT_ID = 123456             # BOT_TOKEN in .env.example


async def import_ranges(conn: asyncpg.Connection, chat_id: int) -> list[tuple[int, int | None]]:
    """What to import, as (after, before) message ids: everything after the last imported message,
    except the span the nightly purge removed (nestor.retention), so history that already left the
    archive does not come back with a fresh stored_at. Older history that never entered it is imported."""
    imported = await conn.fetchval(
        "SELECT coalesce(max(message_id), 0) FROM messages WHERE chat_id = $1 AND raw ? 'import_source'", chat_id)
    purged = await conn.fetchrow("SELECT first_message_id, message_id FROM retention_marks WHERE chat_id = $1", chat_id)
    if purged is None:
        return [(imported, None)]
    older = [(imported, purged["first_message_id"])] if imported + 1 < purged["first_message_id"] else []
    return [*older, (max(imported, purged["message_id"]), None)]


async def import_history(client: TelegramClient, pool: asyncpg.Pool, entity, chat: dict, *, bot_id: int) -> int:
    """Imports one chat's history oldest-first, without the messages of the bot `bot_id`; returns the
    number of new rows."""
    async with pool.acquire() as conn:
        ranges = await import_ranges(conn, chat["id"])
    log.info("import started", extra={"chat_id": chat["id"], "after_message_id": ranges[0][0]})

    inserted = seen = 0
    started = time.monotonic()
    batch: list[dict] = []

    async def flush() -> None:
        nonlocal inserted
        async with pool.acquire() as conn, conn.transaction():
            for raw in batch:
                result = await store.save_message(conn, parse_chat(raw["chat"]), parse_message(raw))
                inserted += result is store.Result.INSERTED
        batch.clear()

    for after, before in ranges:
        async for msg in client.iter_messages(entity, reverse=True, min_id=after):
            if before is not None and msg.id >= before:
                break
            if not isinstance(msg, (t.Message, t.MessageService)):
                continue
            if isinstance(msg.from_id, t.PeerUser) and msg.from_id.user_id == bot_id:
                continue
            batch.append(to_bot_api(msg, chat, msg.sender))
            seen += 1
            if len(batch) >= BATCH:
                await flush()
            if seen % 1000 == 0:
                log.info("import progress", extra={"chat_id": chat["id"], "seen": seen, "inserted": inserted,
                                                   "rate_per_s": round(seen / (time.monotonic() - started), 1)})
    if batch:
        await flush()
    log.info("import finished", extra={"chat_id": chat["id"], "seen": seen, "inserted": inserted})
    return inserted


async def import_chat(client: TelegramClient, pool: asyncpg.Pool, chat_id: int, *, bot_id: int) -> None:
    entity = await client.get_entity(chat_id)
    chat = chat_dict(entity)
    if chat["type"] == "group":
        # Message ids in basic groups are numbered per account, so ours would not match
        # the bot's. As a supergroup the group has one numbering for everyone.
        log.error("basic group: convert it to a supergroup first (e.g. make chat history visible "
                  "to new members), then import the supergroup", extra={"chat_id": chat_id})
        return
    if chat["type"] != "supergroup":
        log.error("not a group, skipped", extra={"chat_id": chat_id, "type": chat["type"]})
        return

    # A supergroup that was upgraded from a basic group keeps the old messages in the
    # old chat; import them under the old chat id first.
    full = await client(functions.channels.GetFullChannelRequest(entity))
    old_id = full.full_chat.migrated_from_chat_id
    if old_id:
        old_entity = await client.get_entity(t.PeerChat(old_id))
        await import_history(client, pool, old_entity, chat_dict(old_entity), bot_id=bot_id)
    await import_history(client, pool, entity, chat, bot_id=bot_id)


async def list_groups(client: TelegramClient, allowed: frozenset[int]) -> None:
    async for dialog in client.iter_dialogs():
        if dialog.is_group:
            entity = dialog.entity
            kind = "supergroup" if isinstance(entity, t.Channel) else "group (convert before import)"
            mark = "*" if utils.get_peer_id(entity) in allowed else " "
            print(f"{mark} {utils.get_peer_id(entity):>16}  {kind:<30} {dialog.name}")
    print("\n* = in ALLOWED_CHAT_IDS")


def bot_id_from(env) -> int:
    """The bot's id, by which its own messages are told and left out: BOT_ID, or the number before the
    colon in BOT_TOKEN. Not .env.example's placeholder: then the bot's messages would be imported."""
    value = env.get("BOT_ID") or env.get("BOT_TOKEN", "").split(":", 1)[0]
    try:
        bot_id = int(value)
    except ValueError:
        bot_id = None
    if bot_id is None or bot_id == PLACEHOLDER_BOT_ID:
        raise SystemExit("missing BOT_ID: the number before the colon in the bot's token (the token itself is not "
                         "needed here); the bot's own messages are told by it and not imported")
    return bot_id


async def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m nestor.importer", description=__doc__.split("\n\n")[0])
    parser.add_argument("--list", action="store_true", help="list your groups with chat ids and exit")
    parser.add_argument("--chat", type=int, action="append", help="chat id to import (default: ALLOWED_CHAT_IDS)")
    parser.add_argument("--session", default="nestor-import", help="Telethon session file name")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        api_id, api_hash = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    except KeyError as e:
        raise SystemExit(f"missing {e.args[0]}: get it at https://my.telegram.org → API development tools") from None
    bot_id = None if args.list else bot_id_from(os.environ)     # --list imports nothing: no bot to tell apart
    allowed = _parse_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))

    async with TelegramClient(args.session, api_id, api_hash) as client:
        if args.list:
            await list_groups(client, allowed)
            return
        chats = args.chat or sorted(allowed)
        if not chats:
            raise SystemExit("nothing to import: pass --chat or set ALLOWED_CHAT_IDS")
        not_allowed = [c for c in chats if c not in allowed]
        if not_allowed:
            raise SystemExit(f"not in ALLOWED_CHAT_IDS, refusing to import: {not_allowed}")
        await client.get_dialogs()  # fills the entity cache so ids resolve
        pool = await store.create_pool(os.environ["DATABASE_URL"])
        try:
            for chat_id in chats:
                await import_chat(client, pool, chat_id, bot_id=bot_id)
        finally:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
