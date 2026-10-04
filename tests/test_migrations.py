"""The backfill of migrations 0006 and 0007: answers written before quoted_user_ids existed get
the authors they quoted, the way the bot tags them now, and what cannot be attributed any more
leaves the archive the way /forget_me takes it."""
import json
from pathlib import Path

from .factories import CHAT_ID
from .test_answerer import BOT_ID

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
BACKFILL = (MIGRATIONS / "0006_quoted_authors.sql").read_text(encoding="utf-8").partition("-- backfill:")[2] \
    .partition("\n")[2]
CHAINS = (MIGRATIONS / "0007_quoted_chains.sql").read_text(encoding="utf-8")      # quotes along chains of answers
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


async def _msg(conn, mid, uid, text, *, chat_id=CHAT_ID, reply_to=None, quote=None, kind="text", **cols):
    raw = {"message_id": mid, "text": text}
    if reply_to is not None:
        raw["reply_to_message"] = {"message_id": reply_to}
    if quote is not None:
        raw["quote"] = {"text": quote, "position": 0}
    names = ", ".join(cols)
    values = ", ".join(f"${i}" for i in range(8, 8 + len(cols)))
    await conn.execute(
        f"""INSERT INTO messages (chat_id, message_id, user_id, sent_at, reply_to_id, kind, text, raw
                                 {', ' + names if cols else ''})
            VALUES ($1, $2, $3, now() - interval '1 day', $4, $5, $6, $7::jsonb {', ' + values if cols else ''})""",
        chat_id, mid, uid, reply_to, kind, text, json.dumps(raw), *cols.values())


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
