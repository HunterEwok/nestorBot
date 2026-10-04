"""New features through the real dispatcher: reminders, «что я пропустил», fact-check by reply,
link and file search, topics of the week, /export, songs, the digest and the wisdom of the day."""
import asyncio
import json
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from nestor.bot import markup, prompts
from nestor.bot.context import classify
from nestor.bot.scheduler import Scheduler
from nestor.mcp import tools

from .factories import CHAT_ID, message, update
from .test_answerer import BOT_ID, FakeChecker, FakeLLM, make_env_fixture  # noqa: F401 - fixture make_env

TZ = ZoneInfo("Europe/Tallinn")
SPOTIFY = "4" * 22
SONG = json.dumps({"artist": "Кино", "title": "Группа крови",
                   "youtube": ["https://www.youtube.com/watch?v=AAAAAAAAAAA", "https://youtu.be/BBBBBBBBBBB"],
                   "spotify": f"https://open.spotify.com/track/{SPOTIFY}"}, ensure_ascii=False)
WISDOM = json.dumps({"quote": "Счастлив тот, кто счастлив у себя дома.", "author": "Лев Толстой",
                     "source_url": "https://ru.wikiquote.org/wiki/Лев_Толстой", "theme": "дом"}, ensure_ascii=False)


def ago(**delta) -> int:
    return int((datetime.now(timezone.utc) - timedelta(**delta)).timestamp())


def local(days: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(datetime.now(TZ).date() + timedelta(days=days), dtime(hour, minute), TZ)


def bot_answer(sent: dict, text: str = "ответ") -> dict:
    return {"message_id": sent["message_id"], "date": 1_790_000_000, "chat": {"id": CHAT_ID, "type": "supergroup"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Н"}, "text": text}


@pytest.mark.parametrize("question, reply, scenario", [
    ("напомни в пятницу про сбор денег", False, "reminder"),
    ("напомни, о чём говорили вчера", False, "summary"),
    ("включи Кино — Группа крови", False, "music"),
    ("найди песню про белые розы", False, "music"),
    ("что я пропустил?", False, "catchup"),
    ("проверь", True, "factcheck"),
    ("это правда?", False, "factcheck"),
    ("оцени тезисы", True, "theses"),
    ("какую ссылку кидал Петя про налоги", False, "links"),
    ("найди в интернете видео про Марс", False, "web"),
])
def test_classify(question, reply, scenario):
    assert classify(question, reply_target=reply) == scenario


# ---- reminders ----

async def test_reminder_texts_are_escaped(make_env):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, напомни через 2 часа про <b>торт</b> & чай", date=ago())))
    assert bot.sent[-1]["parse_mode"] == "HTML" and "про &lt;b&gt;торт&lt;/b&gt; &amp; чай." in bot.sent[-1]["text"]
    await feed(update("message", message(2, "/reminders", date=ago())))
    assert "&lt;b&gt;торт&lt;/b&gt; &amp; чай" in bot.sent[-1]["text"] and "<b>Твои напоминания" in bot.sent[-1]["text"]


async def test_reminder_set_listed_and_sent_once(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, напомни через 2 часа позвонить маме", date=ago())))
    assert bot.sent[-1]["text"].startswith("✅ Напомню ")
    assert bot.sent[-1]["text"].endswith(": позвонить маме. Отменить: /unremind "
                                         + str(await conn.fetchval("SELECT id FROM reminders")))
    row = await conn.fetchrow("SELECT * FROM reminders")
    assert (row["chat_id"], row["user_id"], row["request_msg_id"], row["text"]) == (CHAT_ID, 111, 1, "позвонить маме")
    assert abs(row["due_at"] - datetime.now(timezone.utc) - timedelta(hours=2)) < timedelta(minutes=1)

    await feed(update("message", message(2, "/reminders", date=ago())))
    assert f"#{row['id']} — " in bot.sent[-1]["text"] and "позвонить маме" in bot.sent[-1]["text"]

    answerer = feed.answerer
    assert await answerer.send_due_reminders(datetime.now(timezone.utc)) == 0
    assert await answerer.send_due_reminders(row["due_at"] + timedelta(seconds=1)) == 1
    fired = bot.sent[-1]
    assert fired["reply_to"] == 1 and fired["parse_mode"] == "HTML"
    assert fired["text"] == '⏰ <a href="tg://user?id=111">User111</a>, напоминаю: позвонить маме'
    assert await answerer.send_due_reminders(row["due_at"] + timedelta(minutes=5)) == 0
    # Reminders repeat the person's own words and /forget_me deletes reminders: the fired one and
    # the list are not archived, the confirmation is removed with the person's messages.
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE user_id = $1 AND text LIKE '⏰%'", BOT_ID) == 0
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE user_id = $1 AND text LIKE '🗓%'", BOT_ID) == 0
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE scenario = 'reminder'") == [111]
    await feed(update("message", message(3, "/forget_me", date=ago())))
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE user_id = $1 AND text LIKE '✅ Напомню%'",
                               BOT_ID) == 0
    assert llm.calls == []


async def test_a_long_list_of_reminders_comes_in_several_messages(make_env, conn):
    bot, llm, feed = make_env(rate_limit=20)
    for i in range(1, 10):
        await feed(update("message", message(i, f"нестор, напомни завтра в 1{i}:00 про {'х' * 440} {i}", date=ago())))
    await feed(update("message", message(20, "/reminders", date=ago())))
    lists = [m["text"] for m in bot.sent
             if m["text"].startswith(prompts.REMINDERS_LIST_HEADER) or m["text"].startswith("•")]
    assert len(lists) >= 2 and all(markup.visible_len(t) <= markup.TELEGRAM_LIMIT for t in lists)
    assert lists[0].startswith(prompts.REMINDERS_LIST_HEADER) and lists[-1].endswith(prompts.REMINDERS_LIST_FOOTER)
    assert sum(t.count("• #") for t in lists) == 9
    assert all(m["parse_mode"] == "HTML" for m in bot.sent)


async def test_unremind_limits_and_forget_me(make_env, conn):
    bot, llm, feed = make_env(admins={333})
    await feed(update("message", message(1, "нестор, напомни завтра в 10:00 про встречу", date=ago())))
    await feed(update("message", message(2, "нестор, напомни завтра в 11:00 про обед", date=ago())))
    first, second = [r["id"] for r in await conn.fetch("SELECT id FROM reminders ORDER BY id")]
    await feed(update("message", message(3, f"/unremind {first}", uid=222, date=ago())))
    assert bot.sent[-1]["text"] == prompts.UNREMIND_NOT_FOUND
    await feed(update("message", message(4, f"/unremind {first}", date=ago())))
    assert bot.sent[-1]["text"] == prompts.UNREMIND_DONE.format(id=first)
    await feed(update("message", message(5, f"/unremind {second}", uid=333, date=ago())))  # an admin of the group
    assert bot.sent[-1]["text"] == prompts.UNREMIND_DONE.format(id=second)

    await feed(update("message", message(6, "нестор, напомни через 2 года про юбилей", date=ago())))
    assert bot.sent[-1]["text"] == prompts.REMINDER_TOO_FAR
    await feed(update("message", message(7, "нестор, напомни послезавтра про билеты", date=ago())))
    assert await conn.fetchval("SELECT count(*) FROM reminders WHERE cancelled_at IS NULL") == 1
    await feed(update("message", message(8, "/forget_me", date=ago())))
    assert await conn.fetchval("SELECT count(*) FROM reminders") == 0


async def test_reminder_in_paused_chat_waits_and_comes_late(make_env, conn):
    bot, llm, feed = make_env(admins={111})
    await feed(update("message", message(1, "нестор, напомни через 5 минут про чайник", date=ago())))
    due = await conn.fetchval("SELECT due_at FROM reminders")
    await feed(update("message", message(2, "/pause", date=ago())))
    assert await feed.answerer.send_due_reminders(due + timedelta(hours=2)) == 0
    await feed(update("message", message(3, "/resume", date=ago())))
    assert await feed.answerer.send_due_reminders(due + timedelta(hours=2)) == 1
    assert "напоминаю про чайник\n(с опозданием: напоминание было назначено " in bot.sent[-1]["text"]
    assert " на в " not in bot.sent[-1]["text"]


async def test_reminder_waits_out_flood_control_and_network_errors(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, напомни через 5 минут про чайник", date=ago())))
    due = await conn.fetchval("SELECT due_at FROM reminders")
    answerer, method = feed.answerer, SendMessage(chat_id=CHAT_ID, text="⏰")

    # Flood control: wait as long as Telegram asks, and it does not count as a failure.
    bot.send_errors = [TelegramRetryAfter(method, "Flood control exceeded", retry_after=600)]
    t = due + timedelta(seconds=1)
    assert await answerer.send_due_reminders(t) == 0
    assert await answerer.send_due_reminders(t + timedelta(minutes=9)) == 0
    # Then the network is down for hours: 1, 2, 4 … minutes between tries, at most an hour.
    t += timedelta(minutes=10)
    bot.send_errors = [TelegramNetworkError(method, "timeout") for _ in range(9)]
    assert await answerer.send_due_reminders(t) == 0
    assert len(bot.send_errors) == 8
    assert await answerer.send_due_reminders(t + timedelta(seconds=30)) == 0
    assert len(bot.send_errors) == 8          # too early for the next try: nothing was sent
    for _ in range(8):
        t += timedelta(hours=1)
        assert await answerer.send_due_reminders(t) == 0
    assert bot.send_errors == []
    assert await answerer.send_due_reminders(t + timedelta(hours=1)) == 1
    assert "напоминаю про чайник\n(с опозданием" in bot.sent[-1]["text"]


async def test_reminder_dropped_after_repeated_failures_or_when_removed(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, напомни через 5 минут про чайник", date=ago())))
    await feed(update("message", message(2, "нестор, напомни через 10 минут про суп", date=ago())))
    kettle, soup = await conn.fetch("SELECT id, due_at FROM reminders ORDER BY id")
    answerer, method = feed.answerer, SendMessage(chat_id=CHAT_ID, text="⏰")

    bot.send_errors = [TelegramForbiddenError(method, "bot was kicked from the group chat")]
    assert await answerer.send_due_reminders(kettle["due_at"] + timedelta(seconds=1)) == 0
    assert await conn.fetchval("SELECT cancelled_at IS NOT NULL FROM reminders WHERE id = $1", kettle["id"])

    t = soup["due_at"]
    bot.send_errors = [TelegramNetworkError(method, "timeout") for _ in range(10)]
    for _ in range(10):
        t += timedelta(hours=1)
        assert await answerer.send_due_reminders(t) == 0
    row = await conn.fetchrow("SELECT attempts, cancelled_at FROM reminders WHERE id = $1", soup["id"])
    assert row["attempts"] == 10 and row["cancelled_at"] is not None
    assert await answerer.send_due_reminders(t + timedelta(hours=1)) == 0


# ---- «что я пропустил» ----

async def test_catchup_covers_the_time_since_my_last_message(make_env):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "старое, до ухода", uid=333, date=ago(hours=4))))
    mine = message(2, "ушёл на обед", date=ago(hours=3))
    mine["from"]["username"] = "user111"
    await feed(update("message", mine))
    await feed(update("message", message(3, "а ты знаешь про отпуск?", uid=222, date=ago(hours=2), reply_to_message=mine)))
    await feed(update("message", message(4, "@user111 где ты?", uid=333, date=ago(hours=1))))
    ask = message(5, "нестор, что я пропустил", date=ago())
    ask["from"]["username"] = "user111"
    await feed(update("message", ask))

    user = llm.calls[0][-1]["content"]
    assert "Запрос от User111 (@user111): Что я пропустил с " in user and "Сценарий: catchup" in user
    period = user.split("<сообщения за период>")[1].split("</сообщения за период>")[0]
    assert "про отпуск" in period and "где ты" in period and "до ухода" not in period
    addressed = user.split("<ответы и упоминания для User111 (@user111)>")[1]
    assert "про отпуск" in addressed and "где ты" in addressed


async def test_catchup_with_nothing_new_skips_the_model(make_env):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "я тут", date=ago(minutes=5))))
    await feed(update("message", message(2, "/catchup", date=ago())))
    assert llm.calls == [] and bot.sent[-1]["text"].startswith("👌 С твоего последнего сообщения (")


# ---- fact-check by reply ----

async def test_factcheck_sends_only_the_replied_message(make_env, conn):
    web = FakeLLM("Неверно: https://nasa.example")
    bot, llm, feed = make_env(web=web)
    await feed(update("message", message(1, "секретный план Пети", uid=333)))
    claim = message(2, "Земля плоская, это доказано", uid=222)
    await feed(update("message", claim))
    await feed(update("message", message(3, "нестор, проверь", reply_to_message=claim)))

    assert llm.calls == [] and bot.sent[-1]["text"].startswith("Неверно")
    assert web.calls[0][0]["content"] == prompts.FACTCHECK_SYSTEM
    sent = "\n".join(m["content"] for m in web.calls[0])
    assert "«Земля плоская, это доказано»" in sent and "Вопрос участника: проверь" in sent
    assert "секретный план" not in sent and "User222" not in sent
    # The verdict restates their claim: /forget_me of its author removes the verdict too.
    assert await conn.fetchrow("SELECT scenario, quoted_user_ids FROM bot_requests") == ("factcheck", [222])

    # A follow-up stays with the web profile and carries the claim, read back from the archive.
    await feed(update("message", message(4, "а источник посвежее?", reply_to_message=bot_answer(bot.sent[-1]))))
    follow = web.calls[1]
    assert follow[0]["content"] == prompts.WEB_SYSTEM and "«Земля плоская, это доказано»" in follow[1]["content"]
    assert follow[2]["content"].startswith("Неверно") and llm.calls == []

    # Only the quoted part, if the asker quoted.
    await feed(update("message", message(5, "нестор, это правда?", uid=444, reply_to_message=claim,
                                         quote={"text": "Земля плоская", "position": 0})))
    assert "Утверждение из чата: «Земля плоская»\n" in web.calls[2][-1]["content"]


async def test_factcheck_refusals(make_env):
    web = FakeLLM("x")
    bot, llm, feed = make_env(web=web)
    claim = message(1, "моё мнение", uid=222)
    await feed(update("message", claim))
    await feed(update("message", message(2, "/forget_me", uid=222)))
    await feed(update("message", message(3, "нестор, проверь", reply_to_message=claim)))
    assert bot.sent[-1]["text"] == prompts.REPLIED_OPTED_OUT
    await feed(update("message", message(4, "нестор, проверь")))
    assert bot.sent[-1]["text"] == prompts.FACTCHECK_HOW
    photo = message(5, None, uid=333, photo=[{"file_id": "p", "file_unique_id": "u", "width": 1, "height": 1}])
    await feed(update("message", photo))
    await feed(update("message", message(6, "нестор, проверь", reply_to_message=photo)))
    assert bot.sent[-1]["text"] == prompts.FACTCHECK_NO_TEXT
    assert web.calls == [] and llm.calls == []


# ---- links and files ----

async def test_link_and_file_search(make_env, conn, reader):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "про налоги тут (https://example.com/taxes).", uid=222)))
    await feed(update("message", message(2, None, uid=222, caption="вот договор",
                                         document={"file_id": "d", "file_unique_id": "du",
                                                   "file_name": "договор аренды.pdf"})))
    await feed(update("message", message(3, "нестор, какую ссылку кидал User222 про налоги")))
    assert "Сценарий: links" in llm.calls[0][-1]["content"] and "find_links" in llm.calls[0][0]["content"]

    await conn.execute("INSERT INTO request_tickets VALUES ('t', $1, NULL, now() + interval '1 min')", CHAT_ID)
    found = await tools.find_links(reader, "t", "налог")
    assert "\n  ссылки: https://example.com/taxes" in found
    assert await tools.find_links(reader, "t", "погода") == "Ничего не найдено."
    assert "[document: договор аренды.pdf] вот договор" in await tools.find_files(reader, "t", "договор")
    assert await tools.find_files(reader, "t", kind="photo") == "Ничего не найдено."


# ---- topics of the week ----

async def test_stats_names_the_longest_branches(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("1. Налоги и бюджет"))
    root = message(1, "Налоги надо снизить", uid=222, date=ago(hours=5))
    await feed(update("message", root))
    second = message(2, "Нет, повысить", uid=333, date=ago(hours=4), reply_to_message=root)
    await feed(update("message", second))
    await feed(update("message", message(3, "Почему?", uid=222, date=ago(hours=3), reply_to_message=second)))
    await feed(update("message", message(4, "Бюджет пуст", uid=444, date=ago(hours=2), reply_to_message=root)))
    other = message(5, "Кто идёт в кино?", uid=333, date=ago(hours=2))
    await feed(update("message", other))
    await feed(update("message", message(6, "Я", uid=444, date=ago(hours=1), reply_to_message=other)))
    await feed(update("message", message(7, "/stats", date=ago())))

    text = bot.sent[-1]["text"]
    assert text.startswith(f"<b>{prompts.STATS_ACTIVITY}</b>")
    assert text.endswith(f'<b>{prompts.STATS_TOPICS}</b>\n1. <a href="https://t.me/c/1000000001/1">Налоги и бюджет</a>'
                         " — 4 сообщения")
    naming = llm.calls[0]
    assert naming[0]["content"] == prompts.TOPICS_SYSTEM and "Бюджет пуст" in naming[1]["content"]
    assert await conn.fetchval("SELECT input_tokens FROM bot_requests WHERE scenario = 'stats'") == 1000
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE scenario = 'stats'") is None

    # Without a name from the model, a branch is called by its first words: its author's own,
    # so their /forget_me removes that /stats answer from the archive.
    llm.answer = ""
    await feed(update("message", message(8, "/stats", date=ago())))
    assert "1. <a href=\"https://t.me/c/1000000001/1\">Налоги надо снизить</a>" in bot.sent[-1]["text"]
    stats_id = bot.sent[-1]["message_id"]
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE answer_msg_ids = ARRAY[$1::bigint]",
                               stats_id) == [222]
    await feed(update("message", message(9, "/forget_me", uid=222, date=ago())))
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", stats_id) == 0


# ---- /export ----

async def test_export_sends_own_messages_privately(make_env, conn):
    bot, llm, feed = make_env(no_private={222})
    await feed(update("message", message(1, "первое моё", date=ago(hours=2))))
    await feed(update("message", message(2, "чужое", uid=333, date=ago(hours=1))))
    await feed(update("message", message(3, "второе моё\nв две строки", date=ago(minutes=30))))
    await feed(update("message", message(4, "/export", date=ago())))

    doc = bot.documents[-1]
    assert doc["chat_id"] == 111 and doc["filename"].startswith("nestor-export-")
    assert doc["caption"] == prompts.EXPORT_CAPTION.format(count=3)  # the /export message itself is archived too
    data = doc["data"].decode()
    assert "=== Test group ===" in data and "https://t.me/c/1000000001/1\n  первое моё" in data
    assert "  второе моё\n  в две строки" in data and "чужое" not in data
    assert bot.sent[-1]["text"] == prompts.EXPORT_SENT
    await feed(update("message", message(5, "/export", date=ago())))
    assert bot.sent[-1]["text"] == prompts.EXPORT_TOO_OFTEN and len(bot.documents) == 1

    await feed(update("message", message(6, "/export", uid=222, date=ago())))
    assert bot.sent[-1]["text"] == prompts.EXPORT_START.format(username="ewok_nestor_bot")
    statuses = [r["status"] for r in await conn.fetch("SELECT status FROM bot_requests ORDER BY id")]
    assert statuses == ["ok", "rate_limited", "blocked"]


async def test_private_chat_serves_only_export_and_is_never_archived(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "в группе", uid=222, date=ago())))
    me = {"id": 222, "type": "private", "first_name": "User222"}
    await feed(update("message", message(10, "/start export", uid=222, chat=me, date=ago())))
    assert bot.documents[-1]["chat_id"] == 222 and "в группе" in bot.documents[-1]["data"].decode()
    stranger = {"id": 333, "type": "private", "first_name": "U"}
    await feed(update("message", message(11, "нестор, привет", uid=333, chat=stranger, date=ago())))
    assert (bot.sent[-1]["chat_id"], bot.sent[-1]["text"]) == (333, prompts.PRIVATE_HELP)
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id > 0") == 0
    assert await conn.fetchval("SELECT count(*) FROM chats WHERE chat_id > 0") == 0
    assert llm.calls == []


# ---- songs ----

async def test_song_card_has_the_player_button_when_it_is_set_up(make_env):
    checker = FakeChecker(videos={"BBBBBBBBBBB": "КИНО - Группа крови"})
    bot, llm, feed = make_env(web=FakeLLM("web"), smart=FakeLLM(SONG), checker=checker, music_player_app="play")
    await feed(update("message", message(2, "нестор, включи Кино — Группа крови")))
    card = bot.sent[-1]
    assert card["reply_markup"].inline_keyboard[0][0].url == "https://t.me/ewok_nestor_bot/play?startapp=BBBBBBBBBBB"
    assert card["text"].endswith(prompts.MUSIC_PLAY_APP)


async def test_song_card_plays_in_telegram(make_env, conn):
    smart, web = FakeLLM(SONG), FakeLLM("web")
    checker = FakeChecker(videos={"BBBBBBBBBBB": "КИНО - Группа крови"}, spotify={SPOTIFY: "Группа крови"},
                          apple="https://music.apple.com/ru/album/x/1?i=2")
    bot, llm, feed = make_env(web=web, smart=smart, checker=checker)
    await feed(update("message", message(1, "секретный план Пети", uid=333)))
    await feed(update("message", message(2, "нестор, включи Кино — Группа крови")))

    card = bot.sent[-1]
    youtube = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
    assert card["parse_mode"] == "HTML" and card["reply_to"] == 2
    # The video is only the preview, above the text: a text link would open YouTube outside Telegram.
    assert card["text"] == f"🎵 <b>Кино — Группа крови</b>\n{prompts.MUSIC_PLAY}" and "href" not in card["text"]
    options = card["link_preview_options"]
    assert (options.url, options.prefer_large_media, options.show_above_text) == (youtube, True, True)
    urls = [b.url for row in card["reply_markup"].inline_keyboard for b in row]
    assert urls[:3] == ["https://music.apple.com/ru/album/x/1?i=2", "https://music.youtube.com/watch?v=BBBBBBBBBBB",
                        f"https://open.spotify.com/track/{SPOTIFY}"]
    assert urls[3].startswith("https://music.yandex.ru/search?text=")
    prompt = smart.calls[0]
    assert prompt[0]["content"] == prompts.MUSIC_SYSTEM and prompt[-1]["content"] == "Запрос: включи Кино — Группа крови"
    assert "секретный" not in "\n".join(m["content"] for m in prompt)
    assert llm.calls == [] and web.calls == []
    req = await conn.fetchrow("SELECT * FROM bot_requests")
    assert (req["scenario"], req["status"], req["model"]) == ("music", "ok", "claude-test")
    assert f"YouTube: {youtube}" in req["answer"]

    await feed(update("message", message(3, "а концертную версию?", reply_to_message=bot_answer(card))))
    follow = smart.calls[1]
    assert [m["role"] for m in follow] == ["system", "user", "assistant", "user"]
    assert follow[1]["content"] == "включи Кино — Группа крови" and follow[2]["content"].startswith("Кино — Группа крови")


async def test_song_from_the_replied_message_and_daily_limit(make_env):
    smart = FakeLLM('{"question": "Какую версию: студийную или концертную?"}')
    bot, llm, feed = make_env(smart=smart, music_daily_limit=1)
    await feed(update("message", message(1, "секретный план Пети", uid=333)))
    favourite = message(2, "обожаю «Хочешь» Земфиры", uid=222)
    await feed(update("message", favourite))
    await feed(update("message", message(3, "нестор, включи эту песню", reply_to_message=favourite)))
    assert bot.sent[-1]["text"] == "Какую версию: студийную или концертную?"
    assert smart.calls[0][-1]["content"] == ("Запрос: включи эту песню\n"
                                             "Песня из сообщения, на которое ответили: «обожаю «Хочешь» Земфиры»")
    await feed(update("message", message(4, "нестор, включи Земфира — Хочешь", uid=444)))
    assert bot.sent[-1]["text"] == prompts.MUSIC_LIMIT.format(limit=1) and len(smart.calls) == 1


async def test_song_falls_back_to_the_web_profile(make_env):
    web = FakeLLM("не json")
    bot, llm, feed = make_env(web=web)
    await feed(update("message", message(1, "нестор, включи что-нибудь весёлое")))
    assert web.calls[0][0]["content"] == prompts.MUSIC_SYSTEM and bot.sent[-1]["text"] == prompts.MUSIC_NOT_FOUND
    await feed(update("message", message(2, "нестор, включи", uid=222)))
    assert bot.sent[-1]["text"] == prompts.MUSIC_NO_TEXT and len(web.calls) == 1


async def test_song_without_internet(make_env):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, включи Кино")))
    assert bot.sent[-1]["text"] == prompts.NO_WEB and llm.calls == []


# ---- the digest and the wisdom of the day ----

async def test_digest_is_switched_on_by_an_admin_and_posted_once(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("• отпуск: решили ехать в Грецию"), admins={111})
    await feed(update("message", message(1, "/digest on", uid=222)))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(update("message", message(2, "/digest on")))
    assert markup.to_plain(bot.sent[-1]["text"]).startswith("☀️ Ежедневный дайджест включён")
    yesterday = int(local(-1, 12).timestamp())
    for i in range(10, 16):
        await feed(update("message", message(i, f"обсуждаем отпуск {i}", uid=200 + i, date=yesterday + i)))

    answerer = feed.answerer
    await answerer.run_daily_posts(local(0, 8).astimezone(timezone.utc))      # before DIGEST_TIME
    await answerer.drain()
    assert llm.calls == []
    await answerer.run_daily_posts(local(0, 9, 30).astimezone(timezone.utc))
    await answerer.drain()
    post = bot.sent[-1]
    assert post["reply_to"] is None and post["text"].startswith("☀️ <b>Дайджест за вчера, ")
    assert post["text"].endswith("решили ехать в Грецию") and post["parse_mode"] == "HTML"
    assert llm.calls[0][0]["content"] == prompts.DIGEST_SYSTEM and "обсуждаем отпуск 10" in llm.calls[0][1]["content"]
    count = len(bot.sent)
    await answerer.run_daily_posts(local(0, 10).astimezone(timezone.utc))
    await answerer.drain()
    assert len(llm.calls) == 1 and len(bot.sent) == count
    row = await conn.fetchrow("SELECT * FROM daily_posts")
    assert (row["kind"], row["status"], row["message_id"]) == ("digest", "posted", post["message_id"])
    req = await conn.fetchrow("SELECT * FROM bot_requests WHERE scenario = 'digest'")
    assert (req["user_id"], req["trigger_msg_id"], req["input_tokens"]) == (None, None, 1000)


async def test_long_digest_is_one_message_and_a_failed_send_is_retried(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("• тема: " + "очень длинно " * 600))
    yesterday = int(local(-1, 12).timestamp())
    for i in range(10, 16):
        await feed(update("message", message(i, f"обсуждаем отпуск {i}", uid=200 + i, date=yesterday + i)))
    await conn.execute("UPDATE chats SET settings = settings || jsonb_build_object('digest', true) WHERE chat_id = $1",
                       CHAT_ID)
    answerer = feed.answerer
    count = len(bot.sent)
    bot.send_errors = [TelegramNetworkError(SendMessage(chat_id=CHAT_ID, text="x"), "timeout")]
    await answerer.run_daily_posts(local(0, 9, 30).astimezone(timezone.utc))
    await answerer.drain()
    assert len(bot.sent) == count
    assert await conn.fetchval("SELECT status FROM daily_posts") == "failed"

    await conn.execute("UPDATE daily_posts SET updated_at = now() - interval '11 minutes'")
    await answerer.run_daily_posts(local(0, 9, 45).astimezone(timezone.utc))
    await answerer.drain()
    assert len(bot.sent) == count + 1
    post = bot.sent[-1]["text"]
    assert markup.visible_len(post) <= 4096 and post.endswith("…") and post.startswith("☀️ <b>Дайджест за вчера, ")
    assert await conn.fetchval("SELECT status FROM daily_posts") == "posted"


async def test_wisdom_sends_only_themes_to_the_web_model(make_env, conn):
    llm, smart = FakeLLM('{"themes": ["дом", "деньги", "секретный план"]}'), FakeLLM(WISDOM)
    bot, llm, feed = make_env(llm, smart=smart, admins={111}, wisdom_chat_ids=frozenset({CHAT_ID}))
    today = int(local(0, 0, 30).timestamp())
    for i in range(1, 7):
        await feed(update("message", message(i, f"секретный план Пети про дом и деньги {i}", uid=200 + i,
                                             date=today + i)))
    answerer = feed.answerer
    await answerer.run_daily_posts(local(0, 21, 30).astimezone(timezone.utc))
    await answerer.drain()

    assert "секретный план Пети" in llm.calls[0][1]["content"]   # the archive model sees the chat ...
    to_web = "\n".join(m["content"] for m in smart.calls[0])     # ... the web model only the themes
    assert smart.calls[0][0]["content"] == prompts.WISDOM_SYSTEM
    assert "Темы сегодняшних разговоров: дом, деньги." in to_web
    assert "секретный" not in to_web and "Пети" not in to_web
    post = bot.sent[-1]
    assert post["reply_to"] is None and post["text"].startswith(f"<b>{prompts.WISDOM_HEADER}</b>")
    assert "«Счастлив тот, кто счастлив у себя дома.»\n— <i>Лев Толстой</i>" in post["text"]
    row = await conn.fetchrow("SELECT * FROM daily_posts")
    assert (row["status"], row["themes"], row["author"]) == ("posted", ["дом", "деньги"], "Лев Толстой")
    assert sorted(r["scenario"] for r in await conn.fetch("SELECT scenario FROM bot_requests")) == ["wisdom",
                                                                                                    "wisdom_themes"]

    # The next day the model brings the same quote: it is not posted again.
    count = len(bot.sent)
    await answerer.run_daily_posts(local(1, 21, 30).astimezone(timezone.utc))
    await answerer.drain()
    assert len(bot.sent) == count
    assert await conn.fetchval("SELECT status FROM daily_posts WHERE day = $1", local(1, 0).date()) == "failed"

    # A group admin switches it off: nothing is even tried.
    await feed(update("message", message(50, "/wisdom off")))
    assert markup.to_plain(bot.sent[-1]["text"]).startswith("🦉 Мудрость дня выключена")
    calls = len(smart.calls)
    await answerer.run_daily_posts(local(2, 21, 30).astimezone(timezone.utc))
    await answerer.drain()
    assert len(smart.calls) == calls


async def test_scheduled_posts_skip_groups_without_the_bot(make_env, conn):
    smart = FakeLLM(WISDOM)
    bot, llm, feed = make_env(smart=smart, wisdom_chat_ids=frozenset({CHAT_ID}))
    await feed(update("message", message(1, "привет")))
    bot.bot_status = ChatMemberStatus.LEFT
    await feed.answerer.run_daily_posts(local(0, 22).astimezone(timezone.utc))
    await feed.answerer.drain()
    assert smart.calls == [] and llm.calls == []
    assert await conn.fetchval("SELECT status FROM daily_posts") == "skipped"


async def test_scheduler_keeps_going_after_a_failing_step():
    calls = []

    class Answerer:
        async def send_due_reminders(self):
            calls.append("reminders")
            raise RuntimeError("boom")

        async def run_daily_posts(self):
            calls.append("daily")

    task = asyncio.create_task(Scheduler(Answerer(), tick_s=0.01).run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls.count("daily") >= 2 and calls.count("reminders") >= 2
