"""The backfill of migrations 0006 and 0007: answers written before quoted_user_ids existed get
the authors they quoted, the way the bot tags them now, and what cannot be attributed any more
leaves the archive the way /forget_me takes it. And of 0008: when older answers leave by the
retention clock, and the copies of words the archive should not hold."""
from pathlib import Path

from nestor.collector import store
from nestor.collector.parse import parse_chat, parse_message, reduce_raw

from .factories import CHAT_ID, message
from .test_answerer import BOT_ID

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
BACKFILL = (MIGRATIONS / "0006_quoted_authors.sql").read_text(encoding="utf-8").partition("-- backfill:")[2] \
    .partition("\n")[2]
CHAINS = (MIGRATIONS / "0007_quoted_chains.sql").read_text(encoding="utf-8")      # quotes along chains of answers
RETENTION = (MIGRATIONS / "0008_retention_and_ops.sql").read_text(encoding="utf-8").partition("-- backfill:")[2] \
    .partition("\n")[2]
OLD_ID = -500                       # the basic group the chat was migrated from: ids start from 1 again
OTHER_ID = -1001000000009
INTERNAL = 1000000001


async def _setup(conn):
    await conn.execute("INSERT INTO chats (chat_id, type, migrated_from_chat_id) VALUES ($1, 'supergroup', $2)",
                       CHAT_ID, OLD_ID)
    await conn.execute("INSERT INTO chats (chat_id, type, migrated_to_chat_id) VALUES ($1, 'group', $2)",
                       OLD_ID, CHAT_ID)
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", OTHER_ID)
    for uid, is_bot in ((111, False), (222, False), (333, False), (444, False), (BOT_ID, True)):
        await conn.execute("INSERT INTO users (user_id, first_name, is_bot) VALUES ($1, $2, $3)",
                           uid, f"U{uid}", is_bot)


async def _msg(conn, mid, uid, text, *, chat_id=CHAT_ID, reply_to=None, quote=None, kind="text", extra=None, **cols):
    raw = {"message_id": mid, "text": text}
    if reply_to is not None:
        raw["reply_to_message"] = {"message_id": reply_to}
    if quote is not None:
        raw["quote"] = {"text": quote, "position": 0}
    raw |= extra or {}
    names = ", ".join(cols)
    values = ", ".join(f"${i}" for i in range(8, 8 + len(cols)))
    await conn.execute(
        f"""INSERT INTO messages (chat_id, message_id, user_id, sent_at, reply_to_id, kind, text, raw
                                 {', ' + names if cols else ''})
            VALUES ($1, $2, $3, now() - interval '1 day', $4, $5, $6, $7 {', ' + values if cols else ''})""",
        chat_id, mid, uid, reply_to, kind, text, raw, *cols.values())


async def _req(conn, answer, *, trigger=None, uid=111, scenario="other", answer_ids=None, parent=None, chat_id=CHAT_ID):
    return await conn.fetchval(
        """INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, scenario, question, answer, status,
                                     answer_msg_ids, parent_id)
           VALUES ($1, $2, $3, $4, 'q', $5, 'ok', $6, $7) RETURNING id""",
        chat_id, trigger, uid, scenario, answer, answer_ids, parent)


async def _quoted(conn) -> dict[int, list[int] | None]:
    return {r["id"]: r["quoted_user_ids"] for r in await conn.fetch("SELECT id, quoted_user_ids FROM bot_requests")}


async def test_references_name_the_authors_the_way_the_bot_reads_them(conn):
    await _setup(conn)
    await _msg(conn, 5, 222, "едем в Грецию")
    await _msg(conn, 700, 111, "новая группа 700")
    await _msg(conn, 700, 333, "старая группа 700", chat_id=OLD_ID)
    await _msg(conn, 555, 444, "три цифры")
    await _msg(conn, 5, 444, "другой чат, то же число", chat_id=OTHER_ID)
    await _msg(conn, 900, BOT_ID, "Он писал «едем»")
    cases = {
        "link": await _req(conn, "Он писал [«едем»](#5)", answer_ids=[900]),
        "bare": await _req(conn, "см. #555", answer_ids=[901]),
        "t.me own": await _req(conn, f"Вася: https://t.me/c/{INTERNAL}/5", answer_ids=[902]),
        "t.me other chat": await _req(conn, "Чужой чат: https://t.me/c/1000000009/5", answer_ids=[903]),
        "t.me own 700": await _req(conn, f"Про https://t.me/c/{INTERNAL}/700 спорили", answer_ids=[904]),
        "bare 700 (both groups)": await _req(conn, "Про #700 спорили", answer_ids=[905]),
        "count": await _req(conn, "Это проблема #1 для нас, а #5 не ссылка", answer_ids=[906]),
        "linked count": await _req(conn, "Это проблема #1 для нас, см. [«едем»](#5)", answer_ids=[907]),
        "no such message": await _req(conn, "см. issue #1234", answer_ids=[908]),
        "bot's own message": await _req(conn, "Я уже отвечал: #900", answer_ids=[909]),
    }
    await conn.execute(BACKFILL)
    await conn.execute(CHAINS)
    got = await _quoted(conn)
    assert {name: got[rid] for name, rid in cases.items()} == {
        "link": [222], "bare": [444], "t.me own": [222], "t.me other chat": None, "t.me own 700": [111],
        "bare 700 (both groups)": [111, 333], "count": None, "linked count": [222], "no such message": None,
        "bot's own message": [222]}
    # Nobody has used /forget_me: an unresolved reference is a number, the answer stays.
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE answer IS NULL") == 0
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = 900") == 1


async def test_answers_about_a_replied_message_and_conversations_inherit_authors(conn):
    await _setup(conn)
    # A reminder confirmation repeats the asker's words.
    await _msg(conn, 10, 222, "нестор, напомни завтра в 10:00 про встречу с Машей")
    await _msg(conn, 11, BOT_ID, "✅ Напомню завтра в 10:00: встречу с Машей. Отменить: /unremind 1", reply_to=10)
    reminder = await _req(conn, "✅ Напомню завтра в 10:00: встречу с Машей. Отменить: /unremind 1", trigger=10,
                          uid=222, scenario="reminder", answer_ids=[11])
    # A fact-check verdict: 111 replied «проверь» to 222's claim.
    await _msg(conn, 20, 222, "Земля плоская, это доказано")
    await _msg(conn, 21, 111, "нестор, проверь", reply_to=20)
    await _msg(conn, 22, BOT_ID, "Неверно. «Земля плоская, это доказано» не подтверждается.", reply_to=21)
    verdict = await _req(conn, "Неверно. «Земля плоская, это доказано» не подтверждается.", trigger=21,
                         scenario="factcheck", answer_ids=[22])
    # A follow-up to the verdict, and a follow-up to that: the chain inherits.
    await _msg(conn, 23, 111, "а источник?", reply_to=22)
    await _msg(conn, 24, BOT_ID, "NASA: плоская Земля опровергнута.", reply_to=23)
    follow = await _req(conn, "NASA: плоская Земля опровергнута.", trigger=23, scenario="web", answer_ids=[24],
                        parent=verdict)
    await _msg(conn, 25, 111, "ещё?", reply_to=24)
    await _msg(conn, 26, BOT_ID, "ESA тоже.", reply_to=25)
    follow2 = await _req(conn, "ESA тоже.", trigger=25, scenario="web", answer_ids=[26], parent=follow)
    # Theses about a replied message, and a reply to the first message of a forum topic (nobody's).
    await _msg(conn, 30, 222, "Налоги надо снизить")
    await _msg(conn, 31, 111, "нестор, оцени тезисы", reply_to=30)
    theses = await _req(conn, "Тезис противоречив.", trigger=31, scenario="theses", answer_ids=[32])
    await _msg(conn, 40, 333, "Тема создана", kind="service", thread_id=40, is_topic_message=True)
    await _msg(conn, 41, 111, "нестор, что скажешь?", reply_to=40, thread_id=40, is_topic_message=True)
    topic = await _req(conn, "Скажу так.", trigger=41, answer_ids=[42])
    # A web question in reply to someone: the web profile never saw their message.
    await _msg(conn, 45, 111, "нестор, найди в интернете, кто прав", reply_to=30)
    web = await _req(conn, "Вот что пишут.", trigger=45, scenario="web", answer_ids=[46])
    # An answer that refers to the bot's own earlier answer quotes whoever it quoted.
    repeat = await _req(conn, "Я уже отвечал: [«Неверно»](#22)", trigger=50, answer_ids=[51])
    await _msg(conn, 51, BOT_ID, "Я уже отвечал: «Неверно»")
    # ...and an answer that refers to that one: the chain carries over to the end (0007).
    again = await _req(conn, "Как я говорил: [«Я уже отвечал»](#51)", trigger=60, answer_ids=[61])
    await conn.execute(BACKFILL)
    await conn.execute(CHAINS)
    got = await _quoted(conn)
    assert (got[reminder], got[verdict], got[follow], got[follow2]) == ([222], [222], [222], [222])
    assert (got[theses], got[topic], got[web], got[repeat], got[again]) == ([222], None, None, [222], [222])
    await conn.execute(CHAINS)                      # running it again changes nothing
    assert await _quoted(conn) == got


async def test_what_cannot_be_attributed_leaves_the_archive_once_somebody_opted_out(conn):
    await _setup(conn)
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 333")     # their messages are gone
    await _msg(conn, 1, 222, "беру палатку")
    await _msg(conn, 2, 111, "нестор, кто что берёт?")
    await _msg(conn, 3, BOT_ID, "Петя: «палатку», Вася: «секретный котелок»", reply_to=2)
    await _msg(conn, 4, 111, "точно", reply_to=3, quote="секретный котелок")
    orphan = await _req(conn, "Петя: [«палатку»](#1), Вася: [«секретный котелок»](#777)", trigger=2, answer_ids=[3])
    await _msg(conn, 5, BOT_ID, "Петя: «палатку»", reply_to=2)
    fine = await _req(conn, "Петя: [«палатку»](#1)", trigger=2, answer_ids=[5])
    # Old fired reminders and /reminders lists were archived; other bot messages stay.
    await _msg(conn, 6, BOT_ID, "⏰ U222, напоминаю: встречу с Машей")
    await _msg(conn, 7, BOT_ID, "Твои напоминания в этой группе:\n#1 — завтра в 10:00: встречу с Машей")
    await conn.execute(BACKFILL)
    await conn.execute(CHAINS)
    got = await _quoted(conn)
    assert got[fine] == [222] and got[orphan] == [222]
    assert await conn.fetchval("SELECT answer FROM bot_requests WHERE id = $1", orphan) is None
    assert await conn.fetchval("SELECT answer FROM bot_requests WHERE id = $1", fine) is not None
    left = {r["message_id"] for r in await conn.fetch("SELECT message_id FROM messages WHERE user_id = $1", BOT_ID)}
    assert left == {5}
    assert "котелок" not in str(await conn.fetchval("SELECT raw FROM messages WHERE message_id = 4"))


async def test_chains_that_lead_to_forgotten_words_leave_the_archive(conn):
    """The state 0006 left, then /forget_me by 333: 0007 finds what still carries their words."""
    await _setup(conn)
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id IN (333, 444)")

    async def _tag(rid, ids):
        await conn.execute("UPDATE bot_requests SET quoted_user_ids = $2 WHERE id = $1", rid, ids)

    # 333's chain: A quoted them, B linked A (0006 tagged both, /forget_me removed both), C linked B.
    first = await _req(conn, None, trigger=100, answer_ids=[101])
    await _tag(first, [333])
    second = await _req(conn, None, trigger=102, answer_ids=[103])
    await _tag(second, [333])
    await _msg(conn, 105, BOT_ID, "Как я говорил: «Я уже отвечал»", reply_to=104)
    third = await _req(conn, "Как я говорил: [«Я уже отвечал»](#103)", trigger=104, answer_ids=[105])
    await _msg(conn, 106, 111, "точно", reply_to=105, quote="Я уже отвечал")
    # 444's reminder, confirmed before they used /forget_me: 0006 tagged it, nothing removed it.
    await _msg(conn, 111, BOT_ID, "✅ Напомню завтра в 10:00: встречу с Машей. Отменить: /unremind 1")
    reminder = await _req(conn, "✅ Напомню завтра в 10:00: встречу с Машей.", trigger=110, uid=444,
                          scenario="reminder", answer_ids=[111])
    await _tag(reminder, [444])
    # An answer 0006 removed for a reference nobody can attribute any more, and a follow-up to it.
    orphan = await _req(conn, None, trigger=120, answer_ids=[121])
    await _msg(conn, 123, BOT_ID, "Да, именно так.", reply_to=122)
    follow = await _req(conn, "Да, именно так.", trigger=122, answer_ids=[123], parent=orphan)
    # 222 is still here: an answer that refers to an answer quoting them stays.
    await _msg(conn, 131, BOT_ID, "Петя писал «палатку»")
    kept = await _req(conn, "Петя писал [«палатку»](#1)", trigger=130, answer_ids=[131])
    await _tag(kept, [222])
    await _msg(conn, 133, BOT_ID, "Как я говорил: «Петя писал»")
    again = await _req(conn, "Как я говорил: [«Петя писал»](#131)", trigger=132, answer_ids=[133])

    await conn.execute(CHAINS)
    got = await _quoted(conn)
    assert (got[third], got[reminder], got[follow], got[kept], got[again]) == ([333], [444], None, [222], [222])
    answers = {r["id"]: r["answer"] for r in await conn.fetch("SELECT id, answer FROM bot_requests")}
    assert answers[third] is answers[reminder] is answers[follow] is None
    assert answers[kept] and answers[again]
    left = {r["message_id"] for r in await conn.fetch("SELECT message_id FROM messages WHERE user_id = $1", BOT_ID)}
    assert left == {131, 133}
    assert "Я уже отвечал" not in str(await conn.fetchval("SELECT raw FROM messages WHERE message_id = 106"))
    await conn.execute(CHAINS)                      # running it again changes nothing
    assert await _quoted(conn) == got


async def _state(conn) -> list:
    """Everything the 0008 backfill may change."""
    return [[dict(r) for r in await conn.fetch(q)] for q in (
        "SELECT id, oldest_source_at FROM bot_requests ORDER BY id",
        "SELECT user_id, username, first_name, last_name FROM users ORDER BY user_id",
        "SELECT chat_id, message_id, text, entities, forward_origin, raw FROM messages ORDER BY chat_id, message_id")]


async def test_0008_backfill_is_idempotent_and_counts_old_requests_from_the_chat_start(conn):
    await _setup(conn)
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 333")
    await _msg(conn, 1, 222, "старое из базовой группы", chat_id=OLD_ID)
    await _msg(conn, 1, 111, "первое в супергруппе")
    await _msg(conn, 2, 111, "переслано", forward_origin={"type": "user", "sender_user": {"id": 333, "first_name": "В"}},
               extra={"forward_origin": {"type": "user", "sender_user": {"id": 333, "first_name": "В"}}})
    await _msg(conn, 3, 111, "ответ", quote="чужое",
               extra={"external_reply": {"message_id": 7, "chat": {"id": OTHER_ID, "title": "Другая"}, "text": "чужое"}})
    await conn.execute("UPDATE messages SET stored_at = now() - interval '200 days' WHERE chat_id = $1", OLD_ID)
    await conn.execute("UPDATE messages SET stored_at = now() - interval '100 days' WHERE chat_id = $1", CHAT_ID)
    here = await _req(conn, "ответ")
    empty = await _req(conn, "ответ", chat_id=OTHER_ID)     # nothing of that chat is archived
    news = await conn.fetchval("INSERT INTO bot_requests (scenario, answer, status) VALUES ('news', 'сводка', 'ok') "
                               "RETURNING id")
    # What ADD COLUMN ... DEFAULT now() gave the rows that existed when 0008 ran.
    await conn.execute("UPDATE bot_requests SET created_at = now() - interval '30 days', oldest_source_at = now()")
    await conn.execute(RETENTION)
    at = {r["id"]: r["oldest_source_at"] for r in await conn.fetch("SELECT id, oldest_source_at FROM bot_requests")}
    created = await conn.fetchval("SELECT created_at FROM bot_requests WHERE id = $1", news)
    # The basic group's history counts for the supergroup it became.
    assert at[here] == await conn.fetchval("SELECT stored_at FROM messages WHERE chat_id = $1", OLD_ID)
    assert at[empty] == at[news] == created
    state = await _state(conn)
    await conn.execute(RETENTION)                   # running it again changes nothing
    assert await _state(conn) == state


async def test_0008_external_reply_matches_parse(conn):
    await _setup(conn)
    petya = {"id": 222, "is_bot": False, "first_name": "Петя", "username": "petya"}
    other = {"id": OTHER_ID, "type": "supergroup", "title": "Другая группа"}
    photo = [{"file_id": "x", "file_unique_id": "y", "width": 1, "height": 1}]
    samples = [
        {"external_reply": {"origin": {"type": "user", "date": 1, "sender_user": petya}, "chat": other,
                            "message_id": 77, "text": "секрет Пети", "photo": photo},
         "quote": {"text": "секрет", "position": 0}},
        {"external_reply": {"origin": {"type": "hidden_user", "date": 1, "sender_user_name": "Аноним"}}},
        {"external_reply": {"origin": {"type": "chat", "date": 1, "sender_chat": other, "author_signature": "админ"},
                            "chat": other, "message_id": 5}},
        {"external_reply": {"origin": {"type": "channel", "date": 1, "message_id": 9, "author_signature": "редактор",
                                       "chat": {"id": -1001000000777, "type": "channel", "title": "Канал"}}}},
        # A replied message here as well: the quote is of that one and stays.
        {"reply_to_message": {"message_id": 1, "date": 1, "from": {"id": 222}},
         "external_reply": {"origin": {"type": "user", "date": 1, "sender_user": petya}, "message_id": 3},
         "quote": {"text": "своё", "position": 0}},
        # The replied message was deleted: Telegram sends the quote alone, nobody's any more.
        {"quote": {"text": "чьё-то", "position": 0}},
    ]
    for mid, sample in enumerate(samples, 1):
        sample |= {"message_id": mid, "text": "ответ"}
        await _msg(conn, mid, 111, "ответ", extra=sample)
    await conn.execute(RETENTION)
    stored = {r["message_id"]: r["raw"] for r in await conn.fetch("SELECT message_id, raw FROM messages")}
    assert [stored[mid] for mid in range(1, len(samples) + 1)] == [reduce_raw(sample) for sample in samples]
    assert stored[5]["quote"]["text"] == "своё"
    left = str(stored)
    assert [w for w in ("секрет", "petya", "Аноним", "админ", "редактор", "Канал", "чьё-то") if w in left] == []


async def test_0008_scrubs_forwards_and_quotes_of_opted_out(conn):
    await _setup(conn)
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 333")
    vasya = {"type": "user", "date": 1_700_000_000,
             "sender_user": {"id": 333, "is_bot": False, "first_name": "Вася", "username": "vasya"}}
    bold = [{"type": "bold", "offset": 0, "length": 6}]
    await _msg(conn, 1, 111, "секрет Васи", entities=bold, forward_origin=vasya,
               extra={"entities": bold, "forward_origin": vasya,
                      "forward_from": {"id": 333, "first_name": "Вася", "username": "vasya"}})
    petya = vasya | {"sender_user": {"id": 222, "is_bot": False, "first_name": "Петя"}}
    await _msg(conn, 2, 111, "мысль Пети", forward_origin=petya, extra={"forward_origin": petya})
    # A reply that came after Вася's /forget_me, quoting the message Telegram still showed.
    await _msg(conn, 3, 111, "согласен", reply_to=900, quote="секрет Васи",
               extra={"reply_to_message": {"message_id": 900, "from": {"id": 333}}})
    # Imported replies do not name the replied author: a quote stays only beside an archived message.
    await _msg(conn, 4, 222, "мой текст")
    await _msg(conn, 5, 111, "да", reply_to=4, quote="мой текст", extra={"import_source": "telegram_import"})
    await _msg(conn, 6, 111, "да", reply_to=901, quote="забытое", extra={"import_source": "telegram_import"})
    # Imported forwards name their author by an id; the id of a chat is not a person's.
    imported = {"type": "user", "date": 1_700_000_000, "sender_id": 333}
    await _msg(conn, 7, 111, "секрет Васи, из истории", forward_origin=imported,
               extra={"forward_origin": imported, "import_source": "telegram_import"})
    channel = {"type": "chat", "date": 1_700_000_000, "sender_id": 333}
    await _msg(conn, 8, 111, "из канала", forward_origin=channel, extra={"forward_origin": channel})
    await conn.execute(RETENTION)
    rows = {r["message_id"]: r for r in await conn.fetch("SELECT * FROM messages WHERE chat_id = $1", CHAT_ID)}
    forward = rows[1]
    assert (forward["user_id"], forward["text"], forward["entities"]) == (111, None, None)
    assert forward["forward_origin"] == forward["raw"]["forward_origin"] == {"type": "user", "sender_user": {"id": 333}}
    assert not {"text", "entities"} & set(forward["raw"]) and "Вася" not in str(forward["raw"])
    assert "vasya" not in str(forward["raw"]) and "forward_from" not in forward["raw"]
    assert (rows[7]["text"], rows[7]["forward_origin"]) == (None, {"type": "user", "sender_user": {"id": 333}})
    assert "секрет" not in str(rows[7]["raw"]) and "sender_id" not in str(rows[7]["raw"])
    assert (rows[8]["text"], rows[8]["forward_origin"]) == ("из канала", channel)
    assert (rows[2]["text"], rows[2]["forward_origin"], rows[2]["raw"]["forward_origin"]) == ("мысль Пети", petya, petya)
    assert "quote" not in rows[3]["raw"] and "quote" not in rows[6]["raw"]
    assert rows[5]["raw"]["quote"]["text"] == "мой текст"
    names = await conn.fetchrow("SELECT username, first_name, last_name FROM users WHERE user_id = 333")
    assert tuple(names) == (None, None, None)


async def test_0008_scrubs_what_was_stored_the_way_a_new_message_is_stored(conn):
    """The backfill and store.save_message apply one rule to forwards and quotes: the same words go."""
    await _setup(conn)
    vasya = {"id": 333, "is_bot": False, "first_name": "Вася", "username": "vasya"}
    live = {"type": "user", "date": 1_700_000_000, "sender_user": vasya}
    quote = {"text": "секрет Васи", "position": 0}
    samples = [
        ("секрет Васи", {"forward_origin": live, "forward_from": vasya, "forward_date": 1_700_000_000,
                         "entities": [{"type": "bold", "offset": 0, "length": 6}]}),
        ("секрет Васи", {"forward_origin": {"type": "user", "date": 1_700_000_000, "sender_id": 333}}),
        ("из канала", {"forward_origin": {"type": "chat", "date": 1_700_000_000, "sender_id": 333}}),
        ("мысль Пети", {"forward_origin": live | {"sender_user": {"id": 222, "is_bot": False, "first_name": "Петя"}}}),
        ("да", {"reply_to_message": {"message_id": 900, "date": 1, "from": vasya}, "quote": quote}),
        ("да", {"reply_to_message": {"message_id": 900}, "quote": quote}),           # imported, replied is not archived
        ("да", {"reply_to_message": {"message_id": 1}, "quote": {"text": "мой текст"}}),     # ... is archived
        ("да", {"reply_to_message": {"message_id": 1}}),
    ]
    await _msg(conn, 1, 222, "мой текст")
    rows = [parse_message(message(100 + i, text, **extra)) for i, (text, extra) in enumerate(samples)]
    for row in rows:                                    # as an older collector stored them
        await conn.execute(store._INSERT_MESSAGE, *store._message_args(row))
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 333")
    await conn.execute(RETENTION)

    async def stored():
        return [dict(r) for r in await conn.fetch(
            "SELECT message_id, text, entities, forward_origin, raw FROM messages WHERE message_id >= 100 "
            "ORDER BY message_id")]
    backfilled = await stored()
    await conn.execute("DELETE FROM messages WHERE message_id >= 100")
    for i, (text, extra) in enumerate(samples):         # the same messages arriving now
        await store.save_message(conn, parse_chat(message(1)["chat"]), parse_message(message(100 + i, text, **extra)))
    assert await stored() == backfilled
    assert backfilled[0]["text"] is None and "vasya" not in str(backfilled[0]["raw"])
    assert backfilled[1]["text"] is None and backfilled[2]["text"] == "из канала" and backfilled[3]["text"] == "мысль Пети"
    assert "quote" not in backfilled[4]["raw"] and "quote" not in backfilled[5]["raw"]
    assert backfilled[6]["raw"]["quote"]["text"] == "мой текст"
