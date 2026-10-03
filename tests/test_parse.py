from datetime import datetime, timezone

from nestor.collector.parse import parse_message

from .factories import FORUM_ID, chat, message, user


def test_text_message():
    m = parse_message(message(5, "тезисы Пети", entities=[{"type": "bold", "offset": 0, "length": 6}]))
    assert (m.chat_id, m.message_id, m.kind, m.text) == (-1001000000001, 5, "text", "тезисы Пети")
    assert m.sent_at == datetime.fromtimestamp(1_790_000_000, timezone.utc)
    assert m.entities[0]["type"] == "bold"
    assert m.user_id == 111 and [u.user_id for u in m.users] == [111]
    assert m.edited_at is None


def test_photo_caption_takes_largest_size():
    photo = [{"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
             {"file_id": "big", "file_unique_id": "b", "width": 1280, "height": 1280}]
    m = parse_message(message(6, None, photo=photo, caption="смотрите"))
    assert (m.kind, m.text, m.file_id, m.file_unique_id) == ("photo", "смотрите", "big", "b")


def test_reply_keeps_only_reference_in_raw():
    parent = message(1, "секретный текст", uid=222)
    m = parse_message(message(2, "ответ", reply_to_message=parent))
    assert m.reply_to_id == 1
    assert m.raw["reply_to_message"] == {"message_id": 1, "date": parent["date"], "from": {"id": 222}}
    assert "секретный" not in str(m.raw)


def test_forum_topic_root_is_not_a_reply():
    root = message(10, None, chat_id=FORUM_ID, forum_topic_created={"name": "Тема", "icon_color": 1})
    m = parse_message(message(11, "в теме", chat_id=FORUM_ID, message_thread_id=10,
                              is_topic_message=True, reply_to_message=root))
    assert (m.thread_id, m.is_topic_message, m.reply_to_id) == (10, True, None)

    real = parse_message(message(12, "реплай", chat_id=FORUM_ID, message_thread_id=10,
                                 is_topic_message=True, reply_to_message=message(11, "в теме")))
    assert real.reply_to_id == 11


def test_service_events():
    joined = parse_message(message(20, None, new_chat_members=[user(333), user(444)]))
    assert (joined.kind, joined.event, joined.text) == ("service", "new_chat_members", None)
    assert {u.user_id for u in joined.users} == {111, 333, 444}

    pinned = parse_message(message(21, None, pinned_message=message(3, "закреп", uid=222)))
    assert pinned.event == "pinned_message"
    assert "закреп" not in str(pinned.raw)


def test_anonymous_admin_keeps_sender_chat():
    m = parse_message(message(30, "от имени группы", uid=1087968824,
                              sender_chat=chat(), author_signature="admin"))
    assert m.sender_chat_id == -1001000000001


def test_edit_date_and_forward():
    origin = {"type": "user", "date": 1_700_000_000, "sender_user": user(555)}
    m = parse_message(message(40, "переслано", forward_origin=origin, edit_date=1_790_000_100))
    assert m.forward_origin["sender_user"]["id"] == 555
    assert m.edited_at == datetime.fromtimestamp(1_790_000_100, timezone.utc)
