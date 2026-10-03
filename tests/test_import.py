from datetime import datetime, timezone

import pytest

t = pytest.importorskip("telethon.tl.types")

from nestor.collector.parse import parse_message  # noqa: E402
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


def test_service_event_does_not_clobber_users():
    m = t.MessageService(id=30, peer_id=t.PeerChannel(1000000001), date=WHEN,
                         from_id=t.PeerUser(222), action=t.MessageActionChatAddUser(users=[333]))
    row = parse_message(to_bot_api(m, CHAT, None))
    assert (row.kind, row.event, row.user_id) == ("service", "new_chat_members", 222)
    assert [(u.user_id, u.partial) for u in row.users] == [(222, True)]
    assert row.raw["import_action"] == {"users": [333]}


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
