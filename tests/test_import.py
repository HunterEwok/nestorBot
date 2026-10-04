from datetime import datetime, timezone

import pytest

t = pytest.importorskip("telethon.tl.types")

from nestor import forget, retention
from nestor.collector.parse import parse_message  # noqa: E402
from nestor.collector.store import Result
from nestor.importer.__main__ import import_history, import_ranges
from nestor.importer.convert import chat_dict, to_bot_api  # noqa: E402

from .test_store import save  # noqa: E402

CHANNEL = t.Channel(id=1000000001, title="Друзья", photo=t.ChatPhotoEmpty(), date=None, megagroup=True, forum=True)
CHAT = chat_dict(CHANNEL)
PETYA = t.User(id=222, first_name="Петя", username="petya")
WHEN = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)


def msg(i, text="", **kw):
    return t.Message(id=i, peer_id=t.PeerChannel(1000000001), date=WHEN, message=text, **kw)


def test_chat_dict_uses_bot_api_ids():
    assert CHAT == {"id": -1001000000001, "type": "supergroup", "title": "Друзья", "is_forum": True}


def test_text_message_with_entities():
    m = msg(5, "смотри https://x.y", from_id=t.PeerUser(222),
            entities=[t.MessageEntityUrl(offset=7, length=11), t.MessageEntityTextUrl(0, 6, "https://z")])
    row = parse_message(to_bot_api(m, CHAT, PETYA))
    assert (row.chat_id, row.message_id, row.kind, row.text, row.user_id) == (-1001000000001, 5, "text", "смотри https://x.y", 222)
    assert row.sent_at == WHEN
    assert row.entities == [{"type": "url", "offset": 7, "length": 11},
                            {"type": "text_link", "offset": 0, "length": 6, "url": "https://z"}]
    assert row.users[0].username == "petya" and not row.users[0].partial
    assert row.raw["import_source"] == "telegram_import"


def test_forum_topic_and_reply():
    in_topic = parse_message(to_bot_api(
        msg(11, "в теме", reply_to=t.MessageReplyHeader(forum_topic=True, reply_to_msg_id=10)), CHAT, PETYA))
    assert (in_topic.thread_id, in_topic.is_topic_message, in_topic.reply_to_id) == (10, True, None)
    reply = parse_message(to_bot_api(
        msg(12, "ответ", reply_to=t.MessageReplyHeader(forum_topic=True, reply_to_msg_id=11, reply_to_top_id=10)),
        CHAT, PETYA))
    assert (reply.thread_id, reply.reply_to_id) == (10, 11)


def test_voice_with_caption_and_photo():
    voice = msg(20, "послушай", media=t.MessageMediaDocument(voice=True, document=None))
    row = parse_message(to_bot_api(voice, CHAT, PETYA))
    assert (row.kind, row.text, row.file_id) == ("voice", "послушай", None)
    photo = parse_message(to_bot_api(msg(21, "", media=t.MessageMediaPhoto()), CHAT, PETYA))
    assert (photo.kind, photo.text) == ("photo", None)


def document(*attributes, mime="application/octet-stream"):
    return t.MessageMediaDocument(document=t.Document(
        id=1, access_hash=2, file_reference=b"\x00", date=WHEN, mime_type=mime, size=10, dc_id=2,
        attributes=list(attributes)))


def test_file_name_and_song_tags_are_kept():
    pdf = parse_message(to_bot_api(msg(22, "", media=document(t.DocumentAttributeFilename("отчёт.pdf"))),
                                   CHAT, PETYA))
    assert (pdf.kind, pdf.raw["document"]) == ("document", {"file_name": "отчёт.pdf"})
    song = document(t.DocumentAttributeAudio(duration=200, performer="Кино", title="Группа крови"),
                    t.DocumentAttributeFilename("kino.mp3"), mime="audio/mpeg")
    row = parse_message(to_bot_api(msg(23, "", media=song), CHAT, PETYA))
    assert (row.kind, row.raw["audio"]) == ("audio", {"performer": "Кино", "title": "Группа крови",
                                                      "file_name": "kino.mp3"})
    voice = document(t.DocumentAttributeAudio(duration=3, voice=True), t.DocumentAttributeFilename("v.ogg"))
    voice.voice = True
    assert parse_message(to_bot_api(msg(24, "", media=voice), CHAT, PETYA)).raw["voice"] == {}


def test_service_event_does_not_clobber_users():
    m = t.MessageService(id=30, peer_id=t.PeerChannel(1000000001), date=WHEN,
                         from_id=t.PeerUser(222), action=t.MessageActionChatAddUser(users=[333]))
    row = parse_message(to_bot_api(m, CHAT, None))
    assert (row.kind, row.event, row.user_id) == ("service", "new_chat_members", 222)
    assert [(u.user_id, u.partial) for u in row.users] == [(222, True)]
    assert row.raw["import_action"] == {"users": [333]}


async def test_photo_change_with_binary_fields_is_stored(conn):
    photo = t.Photo(id=7, access_hash=8, file_reference=b"\x00\x01", date=WHEN, sizes=[], dc_id=2)
    m = t.MessageService(id=31, peer_id=t.PeerChannel(1000000001), date=WHEN,
                         from_id=t.PeerUser(222), action=t.MessageActionChatEditPhoto(photo=photo))
    await save(conn, to_bot_api(m, CHAT, None))
    action = await conn.fetchval("SELECT raw->'import_action' FROM messages WHERE message_id = 31")
    assert "file_reference" not in action["photo"] and action["photo"]["date"] == int(WHEN.timestamp())


def test_forward_origin():
    m = msg(40, "переслано", fwd_from=t.MessageFwdHeader(date=WHEN, from_name="Скрытый"))
    row = parse_message(to_bot_api(m, CHAT, PETYA))
    assert row.forward_origin["sender_user_name"] == "Скрытый"


async def test_imported_rows_respect_live_data(conn):
    # Live message already stored: the import must not duplicate or overwrite it.
    live = {"message_id": 5, "date": 1_790_000_000, "chat": dict(CHAT),
            "from": {"id": 222, "is_bot": False, "first_name": "Петр", "username": "petr"}, "text": "живое"}
    await save(conn, live)
    await save(conn, to_bot_api(msg(5, "из импорта", from_id=t.PeerUser(222)), CHAT, None))
    await save(conn, to_bot_api(msg(4, "старое", from_id=t.PeerUser(222)), CHAT, None))
    rows = await conn.fetch("SELECT message_id, text FROM messages ORDER BY message_id")
    assert [(r["message_id"], r["text"]) for r in rows] == [(4, "старое"), (5, "живое")]
    assert await conn.fetchval("SELECT first_name FROM users WHERE user_id = 222") == "Петр"


async def test_importer_resumes_after_purged_history(conn):
    await save(conn, to_bot_api(msg(4, "старое", from_id=t.PeerUser(222)), CHAT, None))
    assert await import_ranges(conn, CHAT["id"]) == [(4, None)]
    # The nightly purge took everything up to #70, the imported messages with it: a new import
    # does not bring that history back with a fresh archive date.
    await conn.execute("DELETE FROM messages")
    await conn.execute("INSERT INTO retention_marks (chat_id, first_message_id, message_id) VALUES ($1, 1, 70)",
                       CHAT["id"])
    assert await import_ranges(conn, CHAT["id"]) == [(70, None)]
    await save(conn, to_bot_api(msg(90, "новое", from_id=t.PeerUser(222)), CHAT, None))
    assert await import_ranges(conn, CHAT["id"]) == [(90, None)]


async def test_importer_leaves_out_only_the_span_the_purge_took(conn):
    await conn.execute("INSERT INTO retention_marks (chat_id, first_message_id, message_id) VALUES ($1, 100, 150)",
                       CHAT["id"])
    assert await import_ranges(conn, CHAT["id"]) == [(0, 100), (150, None)]      # older history was never here
    for imported, expected in ((50, [(50, 100), (150, None)]), (99, [(150, None)]), (120, [(150, None)]),
                               (200, [(200, None)])):
        await conn.execute("DELETE FROM messages")
        await save(conn, to_bot_api(msg(imported, "из истории", from_id=t.PeerUser(222)), CHAT, None))
        assert await import_ranges(conn, CHAT["id"]) == expected


class _History:
    """A Telethon client for a chat that holds messages 1..last: min_id excludes the ids up to it,
    reverse=True is oldest first."""

    def __init__(self, last):
        self.last, self.asked = last, []

    async def iter_messages(self, entity, reverse, min_id):
        self.asked.append(min_id)
        for i in range(min_id + 1, self.last + 1):
            yield msg(i, f"сообщение {i}", from_id=t.PeerUser(222))


async def test_import_after_a_purge_of_live_rows_loads_the_history_before_them_not_the_purged_span(pool, conn):
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT["id"])
    await conn.execute("INSERT INTO users (user_id, first_name) VALUES (222, 'Петя')")
    # The bot collected this chat from #100 on and nothing was ever imported; its first 13 months have left.
    await conn.execute(
        """INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw, stored_at)
           SELECT $1, g, 222, now(), 'x', '{}', now() - CASE WHEN g <= 150 THEN interval '13 months'
                                                                ELSE interval '1 month' END
           FROM generate_series(100, 160) g""", CHAT["id"])
    _, cutoff = await retention.db_times(conn)
    assert (await retention.purge(pool, cutoff, pause_s=0))["messages"] == 51
    client = _History(170)
    assert await import_history(client, pool, None, CHAT) == 99 + 10
    assert client.asked == [0, 150]
    ids = {r["message_id"] for r in await conn.fetch("SELECT message_id FROM messages")}
    assert ids == set(range(1, 100)) | set(range(151, 171))
    assert await import_history(_History(170), pool, None, CHAT) == 0       # and a second run finds nothing new


async def test_import_after_a_purge_by_archive_time_leaves_out_the_whole_span_of_ids(pool, conn):
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT["id"])
    await conn.execute("INSERT INTO users (user_id, first_name) VALUES (222, 'Петя')")
    # Live #9 entered the archive a month before history up to #3 was imported: the purge goes by that
    # time, so #9 leaves first and #3 after it, one batch each. The mark must keep #9.
    await conn.execute(
        """INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw, stored_at)
           VALUES ($1, 9, 222, now(), 'x', '{}', now() - interval '14 months'),
                  ($1, 3, 222, now(), 'x', '{}', now() - interval '13 months')""", CHAT["id"])
    _, cutoff = await retention.db_times(conn)
    assert (await retention.purge(pool, cutoff, batch=1, pause_s=0))["messages"] == 2
    assert await import_ranges(conn, CHAT["id"]) == [(0, 3), (9, None)]
    client = _History(12)
    assert await import_history(client, pool, None, CHAT) == 2 + 3
    assert client.asked == [0, 9]
    ids = {r["message_id"] for r in await conn.fetch("SELECT message_id FROM messages")}
    assert ids == {1, 2, 10, 11, 12}


async def test_imported_forward_of_an_opted_out_author_is_stored_without_text(conn):
    async with conn.transaction():
        await forget.forget(conn, 222, 42)
    forward = msg(2, "секрет Пети", from_id=t.PeerUser(111), entities=[t.MessageEntityBold(0, 6)],
                  fwd_from=t.MessageFwdHeader(date=WHEN, from_id=t.PeerUser(222)))
    assert await save(conn, to_bot_api(forward, CHAT, None)) is Result.INSERTED
    other = msg(3, "мысль Васи", from_id=t.PeerUser(111), fwd_from=t.MessageFwdHeader(date=WHEN, from_id=t.PeerUser(333)))
    await save(conn, to_bot_api(other, CHAT, None))
    rows = {r["message_id"]: r for r in await conn.fetch("SELECT message_id, user_id, text, entities, forward_origin, raw "
                                                         "FROM messages")}
    assert (rows[2]["user_id"], rows[2]["text"], rows[2]["entities"]) == (111, None, None)
    assert rows[2]["forward_origin"] == rows[2]["raw"]["forward_origin"] == {"type": "user", "sender_user": {"id": 222}}
    assert "секрет" not in str(rows[2]["raw"])
    assert rows[3]["text"] == "мысль Васи"


async def test_imported_reply_quoting_a_forgotten_message_is_stored_without_quote(conn):
    # Петя's #10 comes later in the history than his /forget_me: it is not stored, nor is the quote of it.
    async with conn.transaction():
        await forget.forget(conn, 222, 42)
    assert await save(conn, to_bot_api(msg(10, "моё личное", from_id=t.PeerUser(222)), CHAT, None)) is Result.OPTED_OUT
    quoting = t.MessageReplyHeader(reply_to_msg_id=10, quote_text="моё личное")
    await save(conn, to_bot_api(msg(11, "согласен", from_id=t.PeerUser(111), reply_to=quoting), CHAT, None))
    raw = await conn.fetchval("SELECT raw FROM messages WHERE message_id = 11")
    assert "quote" not in raw and "личное" not in str(raw) and raw["text"] == "согласен"
