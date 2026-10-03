from nestor.collector import store
from nestor.collector.parse import parse_chat, parse_message
from nestor.collector.store import Result

from .factories import chat, message, user


async def save(conn, msg, *, is_edit=False):
    return await store.save_message(conn, parse_chat(msg["chat"]), parse_message(msg), is_edit=is_edit)


async def test_insert_is_idempotent(conn):
    msg = message(1, "первый")
    assert await save(conn, msg) is Result.INSERTED
    assert await save(conn, msg) is Result.DUPLICATE
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    row = await conn.fetchrow("SELECT * FROM messages")
    assert row["text"] == "первый" and row["raw"]["from"]["id"] == 111
    assert await conn.fetchval("SELECT title FROM chats") == "Test group"


async def test_edit_keeps_history_and_ignores_redelivery(conn):
    await save(conn, message(1, "было"))
    edit1 = message(1, "стало", edit_date=1_790_000_060)
    edit2 = message(1, "стало ещё раз", edit_date=1_790_000_120)
    assert await save(conn, edit1, is_edit=True) is Result.EDITED
    assert await save(conn, edit1, is_edit=True) is Result.DUPLICATE
    assert await save(conn, edit2, is_edit=True) is Result.EDITED
    assert await save(conn, edit1, is_edit=True) is Result.DUPLICATE  # late, older edit

    assert await conn.fetchval("SELECT text FROM messages") == "стало ещё раз"
    history = await conn.fetch("SELECT old_text FROM message_edits ORDER BY replaced_at")
    assert [r["old_text"] for r in history] == ["было", "стало"]


async def test_edit_of_unknown_message_is_stored(conn):
    assert await save(conn, message(9, "старое, но правленое", edit_date=1_790_000_060), is_edit=True) is Result.INSERTED
    assert await conn.fetchval("SELECT edited_at IS NOT NULL FROM messages WHERE message_id = 9")


async def test_opted_out_user_is_not_stored(conn):
    await save(conn, message(1, "до"))
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 111")
    assert await save(conn, message(2, "после")) is Result.OPTED_OUT
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1


async def test_paused_chat_is_not_stored(conn):
    await save(conn, message(1))
    await conn.execute("UPDATE chats SET enabled = FALSE")
    assert await save(conn, message(2)) is Result.PAUSED


async def test_user_profile_is_updated(conn):
    await save(conn, message(1))
    msg = message(2)
    msg["from"] = user(111, username="petya")
    await save(conn, msg)
    assert await conn.fetchval("SELECT username FROM users WHERE user_id = 111") == "petya"


async def test_group_to_supergroup_migration(conn):
    old_id, new_id = -4000000001, -1004000000001
    allowed = frozenset({old_id})
    await save(conn, message(50, None, chat_id=old_id, migrate_to_chat_id=new_id) | {"chat": chat(old_id, type="group")})
    await save(conn, message(1, None, chat_id=new_id, migrate_from_chat_id=old_id))
    assert await conn.fetchval("SELECT migrated_to_chat_id FROM chats WHERE chat_id = $1", old_id) == new_id
    assert await store.allowed_by_migration(conn, new_id, allowed)
    assert not await store.allowed_by_migration(conn, -100999, allowed)
