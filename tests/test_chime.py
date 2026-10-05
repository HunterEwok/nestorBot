"""The bot's own remarks (/chime on): when it looks, what the model reads, what the code lets through,
how a remark is posted and stored, and «нестор, помолчи»."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import ReactionTypeEmoji

from nestor import retention
from nestor.bot import chime, markup, prompts
from nestor.bot.answerer import _posts
from nestor.bot.chime import ChimeConfig, Pick, parse, trim
from nestor.bot.triggers import BotIdentity

from .factories import CHAT_ID, message, update
from .test_answerer import BOT_ID, FakeLLM, make_env_fixture  # noqa: F401 - make_env

MSK = ZoneInfo("Europe/Moscow")
ME = BotIdentity(BOT_ID, "ewok_nestor_bot", ("нестор", "бот"))
SILENCE = json.dumps({"reply_to": None})
REMARK = "Кстати, первый концерт Боба Марли в Европе был в 1973 году."
YES = json.dumps({"to_bot": True})
PETYA = {"id": 222, "is_bot": False, "first_name": "Петя", "last_name": "Петров", "username": "petya"}
NESTOR = {"id": BOT_ID, "is_bot": True, "first_name": "Нестор", "username": "ewok_nestor_bot"}


def remark(n, text: str = REMARK) -> str:
    return json.dumps({"reply_to": n, "text": text}, ensure_ascii=False)


def noon() -> datetime:
    """Today 12:00 in Moscow: the looks and the chat are timed from it, whatever the clock says now."""
    return datetime.combine(datetime.now(MSK).date(), dtime(12), MSK)


def say(uid: int, mid: int, text: str, at: datetime, **kw) -> dict:
    msg = message(mid, text, uid=uid, date=int(at.timestamp()), **kw)
    if uid == 222:
        msg["from"] = dict(PETYA)
    return update("message", msg)


async def chat(feed, until: datetime, count: int = 10, *, first: int = 1, step_s: int = 60,
               uids=(222, 333, 444)) -> None:
    """`count` messages of people, `step_s` apart, the last one a step before `until`."""
    for i in range(count):
        at = until - timedelta(seconds=step_s * (count - i))
        await feed(say(uids[i % len(uids)], first + i, f"сообщение {first + i}", at))


async def chime_on(conn, **settings) -> None:
    await conn.execute("INSERT INTO chats (chat_id, type, title) VALUES ($1, 'supergroup', 'Test group') "
                       "ON CONFLICT DO NOTHING", CHAT_ID)
    await conn.execute("UPDATE chats SET settings = settings || $2::jsonb WHERE chat_id = $1", CHAT_ID,
                       {"chime": True, **settings})


async def look(feed, at: datetime) -> None:
    await feed.answerer.run_chimes(at.astimezone(UTC))
    await feed.answerer.drain()


async def logged(conn, at: datetime) -> None:
    """The latest row of bot_requests as if it had been written at `at` (rows carry the real clock)."""
    await conn.execute("UPDATE bot_requests SET created_at = $1 WHERE id = (SELECT max(id) FROM bot_requests)", at)


# ---- what code decides ----

def test_hours():
    hours = ChimeConfig().hours
    assert chime.in_hours(dtime(10), hours) and chime.in_hours(dtime(22, 59), hours)
    assert not any(chime.in_hours(at, hours) for at in (dtime(9, 59), dtime(23), dtime(3)))


@pytest.mark.parametrize("answer, expected", [
    (SILENCE, None),
    ("{}", None),
    ('{"reply_to": 2, "text": "  Кстати,\\n это было в 1973 году. "}', Pick(2, "Кстати, это было в 1973 году.")),
    ('Вот: ```json\n{"reply_to": 5, "text": "Ага, так и есть!"}\n```', Pick(5, "Ага, так и есть!")),
    ('{"reply_to": 1, "text": "Т.е. примерно 3.5 млн, т.к. так считали в 1970-х."}',
     Pick(1, "Т.е. примерно 3.5 млн, т.к. так считали в 1970-х.")),
    # Not a number that was shown.
    ('{"reply_to": 6, "text": "Ага"}', None), ('{"reply_to": 0, "text": "Ага"}', None),
    ('{"reply_to": "2", "text": "Ага"}', None), ('{"reply_to": true, "text": "Ага"}', None),
    ('{"reply_to": 2.0, "text": "Ага"}', None),
    # No text.
    ('{"reply_to": 2}', None), ('{"reply_to": 2, "text": "   "}', None), ('{"reply_to": 2, "text": ["Ага"]}', None),
    # Links in any form.
    ('{"reply_to": 2, "text": "Подробности тут: https://example.org/x"}', None),
    ('{"reply_to": 2, "text": "Почитай на erowid.org, там всё есть."}', None),
    ('{"reply_to": 2, "text": "Есть канал t.me/news"}', None),
    ('{"reply_to": 2, "text": "Загляни на www.example"}', None),
    ('{"reply_to": 2, "text": "Есть сайт пример.рф"}', None),
    # The labels the model reads mean nothing in the chat; a signature is dropped.
    ('{"reply_to": 3, "text": "Участник 2 прав: Хофманн синтезировал ЛСД в 1938 году."}', None),
    ('{"reply_to": 3, "text": "Прав участник №2, это был 1938 год."}', None),
    ('{"reply_to": 2, "text": "Нестор: Хофманн синтезировал ЛСД в 1938 году."}',
     Pick(2, "Хофманн синтезировал ЛСД в 1938 году.")),
    ('{"reply_to": 2, "text": "Нестор:"}', None),
    # Not an answer at all.
    ("Скажу: да", None), ("", None), (None, None), ("[1, 2]", None),
])
def test_the_models_answer(answer, expected):
    assert parse(answer, 5) == expected


def test_an_overlong_remark_is_cut_at_a_sentence_end_or_dropped():
    first = "Первые опыты Хофманна с ЛСД были в 1943 году."
    assert trim(first + " " + "А дальше очень длинно и подробно " * 10) == first
    assert trim("Раз. " + "очень " * 60 + "длинно.") is None       # the only end that fits is too early
    assert trim("слово " * 80) is None                              # no sentence end at all
    assert trim("x" * 300) == "x" * 300 and trim("x" * 301) is None
    assert trim(" \n ") is None
    assert parse(remark(1, first + " " + "А дальше очень длинно и подробно " * 10), 3) == Pick(1, first)


@pytest.mark.parametrize("text", ["нестор, помолчи", "Помолчи!", "замолчи уже", "хватит", "Хватит, Нестор!",
                                  "не встревай", "бот, помолчи пожалуйста 🙏", "ну хватит", "помолчи, несторушка"])
def test_asks_quiet(text):
    assert chime.asks_quiet(text, ME)


@pytest.mark.parametrize("text", ["помолчи про это, лучше расскажи", "хватит спорить", "хватит ли нам денег?",
                                  "нестор, кто прав?", "молчи", "не встревай в их спор", "", None,
                                  "хватит?", "Нестор, хватит?", "хватит?!"])     # «is it enough?»
def test_does_not_ask_quiet(text):
    assert not chime.asks_quiet(text, ME)


def test_what_a_remark_may_answer():
    at = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    person = {"user_id": 222, "is_bot": False, "sender_chat_id": None, "sent_at": at - timedelta(minutes=59)}
    ok = {"bot_id": BOT_ID, "previous": [333], "now": at, "max_age": timedelta(minutes=60)}
    assert chime.targetable(person, **ok) and chime.targetable(person, **ok | {"previous": None})
    assert not chime.targetable(None, **ok)                       # deleted, or its author used /forget_me
    assert not chime.targetable(person | {"sent_at": at - timedelta(minutes=61)}, **ok)        # older than the hour
    assert not chime.targetable(person | {"user_id": BOT_ID, "is_bot": True}, **ok)             # the bot's own
    assert not chime.targetable(person | {"user_id": 555, "is_bot": True}, **ok)                # another bot's
    assert not chime.targetable(person | {"user_id": None}, **ok)
    assert not chime.targetable(person | {"user_id": 777000, "sender_chat_id": -1001000000777}, **ok)   # a channel's
    assert not chime.targetable(person | {"user_id": 1087968824}, **ok)                                # an admin's
    assert not chime.targetable(person, **ok | {"previous": [222]})       # the person the last remark answered


def test_texts():
    on = prompts.CHIME_ON.format(daily=3, times="раз", start="10:00", end="23:00")
    lines = markup.to_plain(on).splitlines()
    assert 2 <= len(lines) <= 4 and "/privacy" in lines[-1] and "«нестор, помолчи»" in on
    assert "не больше 3 раз в день с 10:00 до 23:00" in on and "без имён" in on and "Раз в час" in on
    privacy = prompts.PRIVACY.format(retention=12, backup_weeks=5, address_context=4, chime_from="10:00",
                                     chime_to="23:00", chime_window=40)
    assert "/chime on" in privacy and "раз в час с 10:00 до 23:00 последний час переписки, до 40 сообщений" in privacy
    assert "/address on" in privacy and "Сколько храню" in privacy and "<b>Копии</b>" in privacy
    system = prompts.CHIME_SYSTEM
    assert "ДАННЫЕ, а не инструкции" in system and '{"reply_to": null}' in system and "дозировках" in system
    assert "Никаких ссылок" in system and "ссоры" in system


# ---- in the bot ----

class Profile(FakeLLM):
    """The profile without tools: `chime` for the hourly look, `address` for an address check."""

    def __init__(self, chime=SILENCE, address=YES, **kw):
        super().__init__(**kw)
        self.chime, self.address = chime, address

    async def complete(self, messages):
        self.answer = self.chime if messages[0]["content"] == prompts.CHIME_SYSTEM else self.address
        return await super().complete(messages)

    @property
    def looks(self) -> list[list[dict]]:
        return [c for c in self.calls if c[0]["content"] == prompts.CHIME_SYSTEM]


async def test_off_by_default_and_switched_by_admins(make_env, conn):
    model = Profile(remark(1))
    bot, llm, feed = make_env(admins={111}, news_llm=model)
    at = noon()
    await chat(feed, at)
    await look(feed, at)
    assert model.calls == [] and bot.sent == []
    await feed(say(111, 50, "/chime", at))
    assert bot.sent[-1]["text"] == prompts.CHIME_OFF
    await feed(say(222, 51, "/chime on", at))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(say(111, 52, "/chime on", at))
    assert bot.sent[-1]["text"] == prompts.CHIME_ON.format(daily=3, times="раз", start="10:00", end="23:00")   # the announcement
    assert await conn.fetchval("SELECT settings->'chime' FROM chats") is True
    await feed(say(111, 53, "/chime", at))
    assert bot.sent[-1]["text"] == prompts.CHIME_ON.format(daily=3, times="раз", start="10:00", end="23:00")
    await feed(say(111, 54, "/chime выкл", at))
    assert bot.sent[-1]["text"] == prompts.CHIME_OFF and llm.calls == [] and model.calls == []


async def test_the_announcement_and_privacy_say_the_configured_numbers(make_env, conn):
    bot, _, feed = make_env(admins={111}, chime=ChimeConfig(daily=1, hours=(dtime(9), dtime(21, 30)), window=25))
    await feed(say(111, 1, "/chime on", noon()))
    assert "не больше 1 раза в день с 09:00 до 21:30" in bot.sent[-1]["text"]
    await feed(say(111, 2, "/privacy", noon()))
    assert "раз в час с 09:00 до 21:30 последний час переписки, до 25 сообщений" in bot.sent[-1]["text"]


async def test_silence_is_the_default_and_keeps_no_text(make_env, conn):
    model = Profile(SILENCE)
    bot, llm, feed = make_env(news_llm=model)
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    assert len(model.looks) == 1 and llm.calls == [] and bot.sent == [] and bot.reactions == []
    row = await conn.fetchrow("SELECT scenario, user_id, trigger_msg_id, question, answer, input_tokens, status, "
                              "answer_msg_ids, quoted_user_ids, profile FROM bot_requests")
    assert dict(row) == {"scenario": "chime_check", "user_id": None, "trigger_msg_id": None, "question": None,
                         "answer": None, "input_tokens": 1000, "status": "ok", "answer_msg_ids": [],
                         "quoted_user_ids": None, "profile": None}


async def test_a_remark_is_a_reply_stored_as_an_answer_to_its_author(make_env, conn):
    bot, llm, feed = make_env(news_llm=Profile(remark(2)))
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    sent = bot.sent[-1]
    assert (sent["text"], sent["reply_to"], sent["parse_mode"]) == (REMARK, 2, None) and llm.calls == []
    row = await conn.fetchrow("SELECT * FROM bot_requests WHERE scenario = 'chime'")
    assert (row["user_id"], row["trigger_msg_id"], row["question"], row["answer"], row["status"]) == (
        None, None, None, REMARK, "ok")
    assert row["answer_msg_ids"] == [sent["message_id"]] and row["quoted_user_ids"] == [333]
    assert row["oldest_source_at"] == await conn.fetchval("SELECT stored_at FROM messages WHERE message_id = 2")
    archived = await conn.fetchrow("SELECT user_id, text FROM messages WHERE message_id = $1", sent["message_id"])
    assert tuple(archived) == (BOT_ID, REMARK)          # in the archive like the bot's other messages
    assert await conn.fetchval("SELECT count(*) FROM bot_requests") == 1


async def test_the_model_reads_the_last_hour_with_labels_only(make_env, conn):
    model = Profile(SILENCE)
    _, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = noon()
    minute = timedelta(minutes=1)
    await feed(say(222, 1, "старое, больше часа назад", at - 61 * minute))
    await feed(say(555, 2, "мой секрет", at - 50 * minute))
    await feed(say(555, 3, "/forget_me", at - 49 * minute))
    await feed(say(222, 4, "кто знает, когда вышел альбом Kaya?", at - 40 * minute))
    await feed(say(333, 5, "без понятия", at - 39 * minute, reply_to_message=message(4, "кто знает", uid=222)))
    await feed.answerer.save_outgoing(message(6, "Не знаю.", date=int((at - 38 * minute).timestamp()))
                                      | {"from": NESTOR})
    await feed(say(555, 7, "а я вернулся", at - 37 * minute))      # not stored: they used /forget_me
    await chat(feed, at, 8, first=10, uids=(444,))
    await look(feed, at)
    user = model.looks[0][1]["content"]
    expected = ["1. Участник 1: кто знает, когда вышел альбом Kaya?", "2. Участник 2 (ответ на 1): без понятия",
                "3. Нестор: Не знаю."] + [f"{i}. Участник 3: сообщение {i + 6}" for i in range(4, 12)]
    assert user == prompts.CHIME_USER.format(messages="\n".join(expected), n=11)
    for word in ("Петя", "Петров", "petya", "User333", "User444", "секрет", "вернулся", "старое"):
        assert word not in user
    assert model.looks[0][0]["content"] == prompts.CHIME_SYSTEM


async def test_the_model_reads_the_last_chime_window_messages(make_env, conn):
    model = Profile(SILENCE)
    _, _, feed = make_env(news_llm=model, chime=ChimeConfig(window=5))
    await chime_on(conn)
    await chat(feed, noon(), 12)
    await look(feed, noon())
    shown = model.looks[0][1]["content"].split("\n\n")[0].splitlines()
    assert [line.split(": ")[1] for line in shown] == [f"сообщение {i}" for i in range(8, 13)]


async def test_it_looks_only_within_the_hours(make_env, conn):
    model = Profile(SILENCE)
    _, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    day = datetime.now(MSK).date()
    await chat(feed, datetime.combine(day, dtime(10), MSK))
    for outside in (dtime(9, 59), dtime(23), dtime(3)):
        await look(feed, datetime.combine(day, outside, MSK))
    assert model.calls == []
    await look(feed, datetime.combine(day, dtime(10), MSK))
    assert len(model.looks) == 1


def channel_post(mid: int, at: datetime) -> dict:
    """A post of the group's linked channel, as Telegram forwards it into the discussion group."""
    return update("message", message(mid, "Новый пост канала", date=int(at.timestamp())) | {
        "from": {"id": 777000, "is_bot": False, "first_name": "Telegram"}, "is_automatic_forward": True,
        "sender_chat": {"id": -1001000000777, "type": "channel", "title": "Канал"},
        "forward_origin": {"type": "channel", "date": int(at.timestamp()), "message_id": 5,
                           "chat": {"id": -1001000000777, "type": "channel", "title": "Канал"}}})


@pytest.mark.parametrize("people, looks", [(9, 0), (10, 1)])
async def test_it_needs_people_talking_in_the_last_hour(make_env, conn, people, looks):
    model = Profile(SILENCE)
    _, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = noon()
    await chat(feed, at - timedelta(minutes=61), 5, first=100)       # before the hour: not counted
    bot_user = {"id": 555, "is_bot": True, "first_name": "Другой бот"}
    await feed(update("message", message(200, "я тоже бот", date=int(at.timestamp()) - 30) | {"from": bot_user}))
    await feed(channel_post(201, at - timedelta(seconds=20)))       # nor a channel's post
    await chat(feed, at, people)
    await look(feed, at)
    assert len(model.looks) == looks


async def test_once_an_hour_from_the_start_of_the_last_look(make_env, conn):
    model = Profile(SILENCE)
    _, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at, hour, minute = noon(), timedelta(hours=1), timedelta(minutes=1)

    async def talk(until: datetime, first: int) -> None:      # people talk all the hour before
        await chat(feed, until, 20, first=first, step_s=180)

    await talk(at, 1000)
    await look(feed, at)
    await logged(conn, at + timedelta(seconds=10))
    await talk(at + hour, 2000)
    await look(feed, at + 59 * minute)
    assert len(model.looks) == 1
    await look(feed, at + hour)
    assert len(model.looks) == 2
    await logged(conn, at + hour + timedelta(seconds=10))
    # After a restart the log says when the last look was.
    await chat(feed, at + hour + 30 * minute, 10, first=3000, step_s=120)
    _, _, feed = make_env(news_llm=model)
    await look(feed, at + hour + 30 * minute)
    assert len(model.looks) == 2
    await talk(at + 2 * hour, 4000)
    model.chime = remark(5)
    await look(feed, at + 2 * hour + timedelta(seconds=10))
    assert len(model.looks) == 3 and feed.answerer.bot.sent[-1]["text"] == REMARK
    await logged(conn, at + 2 * hour + timedelta(seconds=40))      # the remark went out at 14:00:40
    model.chime = SILENCE
    await talk(at + 3 * hour, 5000)
    await look(feed, at + 3 * hour)                                 # 15:00:00: not an hour since the last look
    assert len(model.looks) == 3
    await look(feed, at + 3 * hour + timedelta(seconds=10))         # 15:00:10: an hour since the look that made it
    assert len(model.looks) == 4


async def test_a_remark_may_come_on_the_next_look(make_env, conn):
    """«No remark in the last hour» counts from the start of the look that made it, not from when the remark
    went out: the next hourly look may speak again."""
    model = Profile(remark(1))                  # message 1 is Петя's
    bot, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at, hour, took = noon(), timedelta(hours=1), timedelta(seconds=40)
    await chat(feed, at)
    await look(feed, at)
    await logged(conn, at + took)               # the model took a while: the remark went out at 12:00:40
    await conn.execute("UPDATE messages SET sent_at = $1 WHERE user_id = $2", at + took, BOT_ID)
    await chat(feed, at + hour, first=100)
    model.chime = remark(3)                     # 1 is the remark itself, 2 Петя's message 100, 3 someone else's 101
    await look(feed, at + hour)
    assert [s["reply_to"] for s in bot.sent] == [1, 101] and len(model.looks) == 2
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE scenario = 'chime' AND status = 'ok'") == 2


@pytest.mark.parametrize("earlier, looks", [
    ([("chime", "ok", 3), ("chime", "ok", 2), ("chime", "ok", 25)], 1),     # two today, one yesterday
    ([("chime", "ok", 3), ("chime", "ok", 2), ("chime", "ok", 1.5)], 0),   # CHIME_DAILY reached
    ([("chime", "error", 3), ("chime", "error", 2), ("chime", "ok", 1.5)], 1),   # never posted: not remarks
    ([("chime_check", "ok", 3), ("chime_check", "ok", 2), ("chime", "ok", 1.5)], 1),   # silences
])
async def test_the_daily_remarks_per_chat(make_env, conn, earlier, looks):
    model = Profile(SILENCE)
    _, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = noon()
    for scenario, status, hours in earlier:
        await conn.execute("INSERT INTO bot_requests (chat_id, scenario, status, created_at) VALUES ($1, $2, $3, $4)",
                           CHAT_ID, scenario, status, at - timedelta(hours=hours))
    await conn.execute("INSERT INTO bot_requests (chat_id, scenario, status) VALUES (-1001000000099, 'chime', 'ok')")
    await chat(feed, at)
    await look(feed, at)
    assert len(model.looks) == looks


async def test_no_look_past_half_the_days_tokens(make_env, conn):
    model = Profile(remark(1))
    bot, _, feed = make_env(news_llm=model, daily_token_limit=10_000)
    await chime_on(conn)
    await conn.execute("INSERT INTO bot_requests (chat_id, scenario, input_tokens, status) "
                       "VALUES ($1, 'x', 5000, 'ok')", CHAT_ID)
    await chat(feed, noon())
    await look(feed, noon())
    assert model.calls == [] and bot.sent == []


@pytest.mark.parametrize("setup", ["paused", "quiet", "off", "not_member", "no_profile"])
async def test_nothing_is_read_when(make_env, conn, setup):
    model = Profile(remark(1))
    bot, llm, feed = make_env(news_llm=None if setup == "no_profile" else model)
    await chime_on(conn)
    await chat(feed, noon())
    if setup == "paused":
        await conn.execute("UPDATE chats SET enabled = FALSE")
    elif setup == "quiet":
        await chime_on(conn, chime_quiet=datetime.now(MSK).date().isoformat())
    elif setup == "off":
        await chime_on(conn, chime=False)
    elif setup == "not_member":
        bot.bot_status = ChatMemberStatus.LEFT
    await look(feed, noon())
    assert model.calls == [] and llm.calls == [] and bot.sent == []
    assert await conn.fetchval("SELECT count(*) FROM bot_requests") == 0


async def test_the_owner_may_route_remarks_to_the_archive_profile(make_env, conn):
    bot, llm, feed = make_env(FakeLLM(remark(1)), routes={"chime": "group"})
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    assert llm.calls[0][0]["content"] == prompts.CHIME_SYSTEM and bot.sent[-1]["text"] == REMARK


@pytest.mark.parametrize("fail, status", [({"fail": True}, "error"), ({"delay": 1}, "timeout")])
async def test_an_error_or_a_timeout_is_silence(make_env, conn, fail, status):
    bot, _, feed = make_env(news_llm=Profile(remark(1), **fail), chime=ChimeConfig(timeout_s=0.05))
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    assert bot.sent == []
    row = await conn.fetchrow("SELECT scenario, status, answer, input_tokens FROM bot_requests")
    assert dict(row) == {"scenario": "chime_check", "status": status, "answer": None, "input_tokens": None}


# Messages 1-3 of people, 4 the bot's own, 5 another bot's, 6 the linked channel's post, 7-13 of people.
@pytest.mark.parametrize("answer", [
    remark(4),                                  # the bot's own message
    remark(5),                                  # another bot's
    remark(6),                                  # the channel's
    remark(14), remark(0), '{"reply_to": "1", "text": "Ага"}',     # not a number it was shown
    remark(1, "очень длинно " * 40),            # too long, nowhere to cut
    remark(1, "Смотри https://example.org"),   # a link
    remark(1, " "),
    "Скажу, что в 1973.",
])
async def test_a_remark_the_code_refuses_is_silence(make_env, conn, answer):
    bot, llm, feed = make_env(news_llm=Profile(answer))
    await chime_on(conn)
    at = noon()
    await chat(feed, at - timedelta(minutes=30), 3)
    await feed.answerer.save_outgoing(message(4, "Кстати, вот ещё.", date=int(at.timestamp()) - 1500) | {"from": NESTOR})
    other = {"id": 555, "is_bot": True, "first_name": "Другой бот"}
    await feed(update("message", message(5, "Я тоже бот", date=int(at.timestamp()) - 1400) | {"from": other}))
    await feed(channel_post(6, at - timedelta(seconds=1300)))
    await chat(feed, at, 7, first=7)
    await look(feed, at)
    assert bot.sent == [] and llm.calls == []
    assert [r["scenario"] for r in await conn.fetch("SELECT scenario FROM bot_requests")] == ["chime_check"]


async def test_it_looks_as_of_when_it_starts(make_env, conn):
    """The look may wait in the group's queue behind the news or the wisdom of the day: the hour, and the
    age of the message it answers, count from when it starts, not from when it was queued."""
    class Picks(Profile):       # answers the message written almost an hour before the tick, when shown it
        async def complete(self, messages):
            shown = messages[-1]["content"].split("\n\n")[0].splitlines()
            n = next((i for i, line in enumerate(shown, 1) if line.endswith("почти час назад")), None)
            self.chime = remark(n) if n else SILENCE
            return await super().complete(messages)

    model = Picks()
    bot, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = noon()
    await feed(say(222, 1, "почти час назад", at - timedelta(minutes=59, seconds=59)))
    await chat(feed, at, first=2)
    feed.answerer._enqueue(_posts(CHAT_ID), lambda: asyncio.sleep(1.2))     # the news, still being written
    await look(feed, at)
    assert len(model.looks) == 1 and bot.sent == []
    assert await conn.fetchval("SELECT scenario FROM bot_requests") == "chime_check"


async def test_a_look_that_waits_past_the_hours_is_dropped(make_env, conn):
    model = Profile(remark(1))
    bot, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = datetime.combine(datetime.now(MSK).date(), dtime(22, 59, 59), MSK)
    await chat(feed, at)
    feed.answerer._enqueue(_posts(CHAT_ID), lambda: asyncio.sleep(1.2))     # the wisdom of the day, being written
    await look(feed, at)
    assert model.calls == [] and bot.sent == []


async def test_never_the_same_person_twice_in_a_row(make_env, conn):
    model = Profile(remark(1))                  # message 1 is Петя's
    bot, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = noon()
    await conn.execute("INSERT INTO bot_requests (chat_id, scenario, status, answer, quoted_user_ids, created_at) "
                       "VALUES ($1, 'chime', 'ok', 'Ага', '{222}', $2)", CHAT_ID, at - timedelta(hours=26))
    await chat(feed, at)
    await look(feed, at)
    assert bot.sent == []
    await chat(feed, at + timedelta(hours=1), first=100)
    model.chime = remark(2)                     # message 101 is someone else's
    await look(feed, at + timedelta(hours=1))
    assert bot.sent[-1]["reply_to"] == 101


@pytest.mark.parametrize("meanwhile", [
    "UPDATE messages SET sent_at = sent_at - interval '2 hours' WHERE message_id = 2",     # older than the hour
    "DELETE FROM messages WHERE message_id = 2",                                           # deleted
    "UPDATE users SET opted_out = TRUE WHERE user_id = 333",                               # its author's /forget_me
    "UPDATE chats SET enabled = FALSE",                                                    # /pause
    "UPDATE chats SET settings = settings || '{\"chime\": false}'",                         # /chime off
])
async def test_what_changes_during_the_look_still_counts(make_env, pool, conn, meanwhile):
    class Slow(Profile):
        async def complete(self, messages):
            async with pool.acquire() as c:
                await c.execute(meanwhile)
            return await super().complete(messages)

    bot, _, feed = make_env(news_llm=Slow(remark(2)))
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    assert bot.sent == []
    assert await conn.fetchval("SELECT scenario FROM bot_requests") == "chime_check"


async def test_a_remark_is_said_to_its_target_or_not_at_all(make_env, conn):
    """Deleted in Telegram before the archive heard of it: Telegram refuses the reply, and the remark
    is not sent without it."""
    bot, _, feed = make_env(news_llm=Profile(remark(2)))
    send, asked = bot.send_message, []

    async def deleted(chat_id, text, reply_parameters=None, **kw):     # what Telegram does then
        asked.append(reply_parameters)
        if not reply_parameters.allow_sending_without_reply:
            raise TelegramBadRequest(SendMessage(chat_id=chat_id, text=text),
                                     "Bad Request: message to be replied not found")
        return await send(chat_id, text, reply_parameters=None, **kw)
    bot.send_message = deleted
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    assert bot.sent == [] and [r.message_id for r in asked] == [2]
    row = await conn.fetchrow("SELECT scenario, status, answer, answer_msg_ids, quoted_user_ids FROM bot_requests")
    assert dict(row) == {"scenario": "chime_check", "status": "error", "answer": None, "answer_msg_ids": [],
                         "quoted_user_ids": None}
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE user_id = $1", BOT_ID) == 0


async def test_one_look_per_chat_at_a_time(make_env, conn):
    model = Profile(SILENCE, delay=0.3)
    _, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    await chat(feed, noon())
    await chat(feed, noon() + timedelta(minutes=61), first=100)
    await feed.answerer.run_chimes(noon())
    await feed.answerer.run_chimes(noon() + timedelta(minutes=61))     # the first one is still reading
    await feed.answerer.drain()
    assert len(model.looks) == 1


async def test_the_authors_forget_me_while_the_remark_is_sent_still_removes_it(make_env, conn):
    bot, _, feed = make_env(news_llm=Profile(remark(2)))
    send = bot.send_message

    async def forgotten_meanwhile(chat_id, text, **kw):
        await feed.answerer.forget_user(333)
        return await send(chat_id, text, **kw)
    bot.send_message = forgotten_meanwhile
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    assert bot.sent[-1]["text"] == REMARK
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE user_id = $1", BOT_ID) == 0
    assert await conn.fetchval("SELECT answer FROM bot_requests WHERE scenario = 'chime'") is None


# ---- «нестор, помолчи» ----

async def posted(feed, conn) -> dict:
    """A remark on message 2 of the last hour: the bot's message as Telegram has it."""
    await chime_on(conn)
    await chat(feed, noon())
    await look(feed, noon())
    sent = feed.answerer.bot.sent[-1]
    assert sent["text"] == REMARK
    await logged(conn, noon() + timedelta(seconds=30))
    return {"message_id": sent["message_id"], "date": int(noon().timestamp()), "from": NESTOR,
            "chat": {"id": CHAT_ID, "type": "supergroup"}, "text": REMARK}


@pytest.mark.parametrize("text, replied", [("нестор, помолчи", True), ("помолчи", True), ("Хватит, Нестор!", True),
                                           ("не встревай", True), ("нестор, помолчи", False)])
async def test_quiet_until_the_end_of_the_day(make_env, conn, text, replied):
    model = Profile(remark(2))
    bot, llm, feed = make_env(news_llm=model)
    sent = await posted(feed, conn)
    now = datetime.now(UTC)
    kw = {"reply_to_message": sent} if replied else {}
    await feed(say(444, 30, text, now, **kw))
    assert bot.reactions[-1] == (30, ["👌"]) and llm.calls == [] and len(bot.sent) == 1
    assert await conn.fetchval("SELECT settings->>'chime_quiet' FROM chats") == datetime.now(MSK).date().isoformat()
    await look(feed, noon() + timedelta(hours=2))
    assert len(model.looks) == 1
    # Answers go on as usual.
    await feed(say(444, 31, "нестор, привет", now))
    assert len(llm.calls) == 1 and bot.sent[-1]["reply_to"] == 31
    # The next day it may speak again.
    await chat(feed, noon() + timedelta(hours=3), first=100)
    await chime_on(conn, chime_quiet=(datetime.now(MSK).date() - timedelta(days=1)).isoformat())
    await look(feed, noon() + timedelta(hours=3))
    assert len(model.looks) == 2


async def test_chime_on_ends_the_quiet(make_env, conn):
    model = Profile(SILENCE)
    _, _, feed = make_env(admins={111}, news_llm=model)
    await chime_on(conn, chime_quiet=datetime.now(MSK).date().isoformat(), address=True)
    await chat(feed, noon())
    await feed(say(111, 50, "/chime off", noon()))
    assert await conn.fetchval("SELECT settings->>'chime_quiet' FROM chats") is not None     # off keeps it
    await feed(say(111, 51, "/chime on", noon()))
    settings = await conn.fetchval("SELECT settings FROM chats")
    assert settings["chime"] is True and settings["address"] is True and "chime_quiet" not in settings
    await look(feed, noon())
    assert len(model.looks) == 1


@pytest.mark.parametrize("replied", [False, True])
async def test_quiet_without_a_comma_needs_no_check_where_address_is_on(make_env, conn, replied):
    model = Profile(remark(2), address=json.dumps({"to_bot": False}))
    bot, llm, feed = make_env(news_llm=model)
    await posted(feed, conn)
    await chime_on(conn, address=True)
    kw = {"reply_to_message": message(2, "сообщение 2", uid=333)} if replied else {}    # under the remark's target
    await feed(say(444, 30, "нестор помолчи", datetime.now(UTC), **kw))
    assert model.calls == model.looks and llm.calls == [] and bot.reactions[-1] == (30, ["👌"])
    assert await conn.fetchval("SELECT settings->>'chime_quiet' FROM chats") == datetime.now(MSK).date().isoformat()


async def test_is_it_enough_is_a_question_not_quiet(make_env, conn):
    bot, llm, feed = make_env()
    await feed(say(222, 1, "нестор, сколько мяса брать на шашлык на пятерых?", datetime.now(UTC)))
    answer = message(bot.sent[-1]["message_id"], "Ответ модели", uid=BOT_ID)
    await feed(say(222, 2, "хватит?", datetime.now(UTC), reply_to_message=answer))
    await feed(say(222, 3, "Нестор, хватит?", datetime.now(UTC)))
    assert len(llm.calls) == 3 and [s["reply_to"] for s in bot.sent] == [1, 2, 3]
    assert await conn.fetchval("SELECT settings->>'chime_quiet' FROM chats") is None


async def test_quiet_says_it_in_words_where_the_chat_takes_no_such_reaction(make_env, conn):
    bot, llm, feed = make_env(news_llm=Profile(remark(2)))
    sent = await posted(feed, conn)
    bot.available_reactions = [ReactionTypeEmoji(emoji="👍")]
    await feed(say(444, 30, "помолчи", datetime.now(UTC), reply_to_message=sent))
    assert bot.sent[-1]["text"] == prompts.CHIME_QUIET and bot.sent[-1]["reply_to"] == 30 and llm.calls == []
    assert await conn.fetchval("SELECT settings->>'chime_quiet' FROM chats") == datetime.now(MSK).date().isoformat()


async def test_quiet_said_to_someone_else_is_not_to_the_bot(make_env, conn):
    bot, llm, feed = make_env(news_llm=Profile(remark(2)))
    await chime_on(conn)
    await feed(say(444, 30, "помолчи", datetime.now(UTC), reply_to_message=message(1, "бла-бла", uid=333)))
    assert bot.reactions == [] and bot.sent == [] and llm.calls == []
    assert await conn.fetchval("SELECT settings->>'chime_quiet' FROM chats") is None


# ---- after a remark ----

async def test_a_reply_to_a_remark_is_answered_and_opens_the_window(make_env, conn):
    model = Profile(remark(2))
    bot, llm, feed = make_env(news_llm=model, chime=ChimeConfig(daily=1))
    sent = await posted(feed, conn)
    await chime_on(conn, address=True)
    now = datetime.now(UTC)
    # The remark answered 333's message: it does not open their window, it is not an answer to a request.
    await feed(say(333, 30, "а ты уверен?", now))
    assert model.calls == model.looks and llm.calls == []
    # A reply to it is a request like any other, not limited by CHIME_DAILY, and the model knows what it replies to.
    await feed(say(444, 31, "а откуда ты знаешь?", now, reply_to_message=sent))
    assert bot.sent[-1]["reply_to"] == 31 and len(llm.calls) == 1
    history = llm.calls[0][1:3]
    assert history == [{"role": "user", "content": prompts.CHIME_TURN}, {"role": "assistant", "content": REMARK}]
    answer = await conn.fetchrow("SELECT scenario, user_id, quoted_user_ids, parent_id FROM bot_requests "
                                 "WHERE trigger_msg_id = 31")
    chimed = await conn.fetchval("SELECT id FROM bot_requests WHERE scenario = 'chime'")
    assert (answer["scenario"], answer["user_id"], answer["parent_id"]) == ("other", 444, chimed)
    assert answer["quoted_user_ids"] == [333]       # it may repeat the remark, and so the words it answered
    # The answer opened 444's window: their «а ты уверен?» right after it may be to the bot.
    await feed(say(444, 32, "а ты уверен?", datetime.now(UTC) + timedelta(seconds=1)))
    assert len(model.calls) == 2 and len(llm.calls) == 2 and bot.sent[-1]["reply_to"] == 32


async def test_forget_me_of_the_targets_author_removes_the_remark(make_env, conn):
    _, _, feed = make_env(news_llm=Profile(remark(2)))
    sent = await posted(feed, conn)
    now = datetime.now(UTC)
    await feed(say(444, 30, "ого, не знал", now, reply_to_message=sent, quote={"text": "Боба Марли", "position": 0}))
    await feed(say(333, 31, "/forget_me", now))
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", sent["message_id"]) == 0
    assert "Марли" not in str(await conn.fetchval("SELECT raw FROM messages WHERE message_id = 30"))
    row = await conn.fetchrow("SELECT answer, answer_msg_ids FROM bot_requests WHERE scenario = 'chime'")
    assert (row["answer"], row["answer_msg_ids"]) == (None, [sent["message_id"]])


async def test_forget_me_of_a_forwarded_messages_author_removes_the_remark(make_env, conn):
    """333 forwarded 888's words into the group; a remark on them may repeat them."""
    _, _, feed = make_env(news_llm=Profile(remark(1)))
    await chime_on(conn)
    at = noon()
    forwarded = {"type": "user", "date": 1_789_000_000, "sender_user": {"id": 888, "is_bot": False, "first_name": "Вася"}}
    await feed(say(333, 1, "Марли умер в 1985", at - timedelta(minutes=30), forward_origin=forwarded))
    await chat(feed, at, first=2)
    await look(feed, at)
    remark_id = feed.answerer.bot.sent[-1]["message_id"]
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE scenario = 'chime'") == [333, 888]
    await feed(say(888, 31, "/forget_me", datetime.now(UTC)))
    row = await conn.fetchrow("SELECT answer, answer_msg_ids FROM bot_requests WHERE scenario = 'chime'")
    assert (row["answer"], row["answer_msg_ids"]) == (None, [remark_id])
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", remark_id) == 0


async def test_a_remark_on_a_dispute_leaves_with_either_side(make_env, conn):
    """A remark on «нет, раньше» may repeat the claim it answers: «Марли умер в 1985» counts as quoted too.
    The next remark may still answer the other side: it was not the one answered."""
    model = Profile(remark(2, "Марли умер в 1981-м, а не в 1985-м."))
    bot, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = noon()
    await feed(say(444, 1, "Марли умер в 1985", at - timedelta(minutes=20)))
    await feed(say(333, 2, "нет, раньше", at - timedelta(minutes=19),
                   reply_to_message=message(1, "Марли умер в 1985", uid=444)))
    await chat(feed, at, first=3, uids=(222, 555))
    await look(feed, at)
    remark_id = bot.sent[-1]["message_id"]
    assert bot.sent[-1]["reply_to"] == 2
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE scenario = 'chime'") == [333, 444]
    await feed(say(444, 50, "Марли — это сила", at + timedelta(minutes=49)))
    await chat(feed, at + timedelta(hours=1), first=100, uids=(222, 555))
    model.chime = remark(1)         # 444's message of the new hour
    await look(feed, at + timedelta(hours=1))
    assert bot.sent[-1]["reply_to"] == 50
    await feed(say(444, 60, "/forget_me", datetime.now(UTC)))
    assert await conn.fetchval("SELECT count(answer) FROM bot_requests WHERE scenario = 'chime'") == 0
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", remark_id) == 0


async def test_the_bots_posts_are_shown_by_their_kind(make_env, conn):
    """A /stats answer names people with their usernames: the model sees only that it was one. A /forget_me
    of anyone it named takes it out of the archive."""
    model = Profile(SILENCE)
    bot, _, feed = make_env(news_llm=model)
    await chime_on(conn)
    at = noon()
    await feed(say(555, 1, "всем привет", at - timedelta(minutes=50)))
    await feed(say(222, 2, "/stats", at - timedelta(minutes=45)))
    stats = bot.sent[-1]["message_id"]
    await conn.execute("UPDATE messages SET sent_at = $1 WHERE message_id = $2", at - timedelta(minutes=44), stats)
    await chat(feed, at, first=3)
    await look(feed, at)
    user = model.looks[0][1]["content"]
    assert "3. Нестор: [статистика]" in user
    for name in ("Петя", "Петров", "petya", "User555"):
        assert name not in user
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE scenario = 'stats'") == [222, 555]
    await feed(say(555, 60, "/forget_me", datetime.now(UTC)))
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", stats) == 0


async def test_the_remark_leaves_the_archive_with_its_target(make_env, pool, conn):
    bot, _, feed = make_env(news_llm=Profile(remark(2)))
    await chime_on(conn)
    await chat(feed, noon())
    await conn.execute("UPDATE messages SET stored_at = now() - interval '13 months' WHERE message_id = 2")
    await look(feed, noon())
    remark_id = bot.sent[-1]["message_id"]
    assert await conn.fetchval("SELECT oldest_source_at < now() - interval '12 months' FROM bot_requests")
    _, cutoff = await retention.db_times(conn)
    got = await retention.purge(pool, cutoff, pause_s=0)
    assert (got["answers"], got["answer_messages"], got["messages"]) == (1, 1, 1)
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = ANY($1::bigint[])", [2, remark_id]) == 0
    assert await conn.fetchval("SELECT answer FROM bot_requests WHERE scenario = 'chime'") is None
