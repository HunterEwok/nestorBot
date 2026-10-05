"""Addressing in one's own words (/address on): what code decides, what goes to the model without
tools, the conversation window after an answer, the caps, and what is shown and stored."""
import asyncio
import json
from datetime import UTC, datetime

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from nestor.bot import markup, prompts
from nestor.bot.addressing import AddressConfig, Addresser, candidate, parse, render
from nestor.bot.reactions import ReactionConfig
from nestor.bot.triggers import BotIdentity, detect

from .factories import CHAT_ID, message, update
from .test_answerer import BOT_ID, FakeLLM, make_env_fixture  # noqa: F401 - make_env

ME = BotIdentity(BOT_ID, "ewok_nestor_bot", ("нестор", "бот"))
YES, NO = json.dumps({"to_bot": True}), json.dumps({"to_bot": False})
PETYA = {"id": 222, "is_bot": False, "first_name": "Петя", "last_name": "Петров", "username": "petya"}


def now() -> int:
    return int(datetime.now(UTC).timestamp())


def verdict(text: str | None, *, on: bool = True, bot_word: bool = True, window: bool = False, **kw) -> str:
    """explicit (answered as today), candidate (asks the model) or silent."""
    msg = message(1, text, **kw)
    if detect(msg, ME, generic_keyword_enabled=bot_word, addressing=on) is not None:
        return "explicit"
    if on and candidate(msg, ME, generic_keyword_enabled=bot_word, window=window) is not None:
        return "candidate"
    return "silent"


# ---- what code decides ----

@pytest.mark.parametrize("text, expected", [
    # The name at the start with a mark after it, or with «ты», a request or a question: as today.
    ("Нестор, привет", "explicit"),
    ("нестор: о чём говорили", "explicit"),
    ("нестор — кто прав", "explicit"),
    ("нестор - кто прав", "explicit"),
    ("нестор!", "explicit"),
    ("Нестор? ты тут", "explicit"),
    ("нестор ты где", "explicit"),
    ("нестор как думаешь, будет дождь", "explicit"),
    ("нестор оцени тезисы", "explicit"),
    ("нестор напомни завтра в 10 про встречу", "explicit"),
    ("нестор вчера тупил?", "explicit"),
    ("бот, привет", "explicit"),
    # The name at the start and nothing that speaks to it: maybe it is talk about the bot.
    ("нестор вчера тупил", "candidate"),
    ("бот опять всё перепутал", "candidate"),
    ("Нестор молодец", "candidate"),
    ("нестор", "candidate"),
    # The name elsewhere with a sign of an address.
    ("слушай, Нестор, а ты как думаешь?", "candidate"),
    ("Ну что, Нестор, пойдём", "candidate"),
    ("как думаешь, нестор?", "candidate"),
    ("спасибо, нестор", "candidate"),
    ("привет нестор!", "candidate"),
    ("а нестор что думает?", "candidate"),
    ("напомни мне завтра, нестор", "candidate"),
    ("несторушка, помоги", "candidate"),
    ("Несторчик, ну скажи", "candidate"),
    ("эй бот, ты тут?", "candidate"),
    ("кто знает, бот это сможет?", "candidate"),
    # The name without any sign: talk about the bot, silence.
    ("вчера нестор тупил", "silent"),
    ("спроси у Нестора, он знает", "silent"),
    ("я с Нестором, кстати, говорил", "silent"),
    ("Нестору виднее", "silent"),
    ("в несторе что-то сломалось", "silent"),
    ("бота опять забанили", "silent"),
    # Not the name at all.
    ("ботинки купил", "silent"),
    ("несторианство — древняя ересь?", "silent"),
    ("а ты уверен?", "silent"),             # nobody was answered a moment ago
    ("слушай, а где ключи?", "silent"),
    ("", "silent"),
    (None, "silent"),
])
def test_with_address_on(text, expected):
    assert verdict(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("нестор вчера тупил", "explicit"),             # the name at the start: as it always was
    ("бот опять всё перепутал", "explicit"),
    ("Нестор, привет", "explicit"),
    ("слушай, Нестор, а ты как думаешь?", "silent"),
    ("как думаешь, нестор?", "silent"),
    ("а нестор что думает?", "silent"),
    ("а ты уверен?", "silent"),
])
def test_with_address_off_nothing_changes(text, expected):
    assert verdict(text, on=False) == expected
    assert verdict(text, on=False, window=True) == expected


@pytest.mark.parametrize("text, expected", [
    ("бот, привет", "silent"),
    ("бот опять всё перепутал", "silent"),
    ("эй бот, ты тут?", "silent"),
    ("кто знает, бот это сможет?", "silent"),
    ("нестор вчера тупил", "candidate"),
    ("слушай, Нестор, а ты как думаешь?", "candidate"),
    ("Нестор, привет", "explicit"),
])
def test_without_the_generic_bot_word(text, expected):
    assert verdict(text, bot_word=False) == expected


@pytest.mark.parametrize("text, expected", [
    ("а ты уверен?", "candidate"),
    ("а почему?", "candidate"),
    ("и что дальше", "candidate"),
    ("А ты сам-то как считаешь", "candidate"),
    ("можешь подробнее", "candidate"),
    ("расскажи ещё про это", "candidate"),
    ("откуда это известно?", "candidate"),
    ("ок", "silent"),
    ("понятно", "silent"),
    ("ахаха", "silent"),
    ("вчера нестор тупил", "silent"),           # the name without a sign stays silent in the window too
    ("Нестор, привет", "explicit"),
])
def test_in_the_window_after_an_answer(text, expected):
    assert verdict(text, window=True) == expected


@pytest.mark.parametrize("kw", [
    {"forward_origin": {"type": "user", "date": 1, "sender_user": PETYA}},
    {"from": {"id": 5, "is_bot": True, "first_name": "Другой бот"}},
    {"sender_chat": {"id": CHAT_ID, "type": "supergroup"}},
    {"via_bot": {"id": 5, "is_bot": True, "first_name": "Другой бот"}},
    {"reply_to_message": message(7, "что-то", uid=222)},                 # said to Петя
])
def test_not_a_candidate_whatever_the_words(kw):
    assert verdict("слушай, Нестор, а ты как думаешь?", **kw) == "silent"
    assert verdict("а ты уверен?", window=True, **kw) == "silent"


def test_replies_that_still_may_be_to_the_bot():
    own = message(7, "я тут подумал", uid=111)
    assert verdict("а ты как думаешь, нестор?", reply_to_message=own) == "candidate"
    topic = message(7, "Тема", uid=222)        # the forum topic itself is not a reply to Петя
    assert verdict("а ты как думаешь, нестор?", reply_to_message=topic, message_thread_id=7,
                   is_topic_message=True) == "candidate"
    channel = message(7, "Пост канала", uid=777000, sender_chat={"id": -1001, "type": "channel"})
    assert verdict("а ты как думаешь, нестор?", reply_to_message=channel) == "candidate"
    assert verdict("/foo, нестор, а ты как думаешь?") == "silent"


@pytest.mark.parametrize("text, request_text", [
    ("как думаешь, нестор?", "как думаешь?"),
    ("спасибо, нестор", "спасибо"),
    ("привет нестор!", "привет!"),
    ("нестор вчера тупил", "вчера тупил"),
    ("Несторушка, помоги с задачей", "помоги с задачей"),
    ("слушай, Нестор, а ты как думаешь?", "слушай, Нестор, а ты как думаешь?"),    # in the middle it stays
    ("а ты уверен?", "а ты уверен?"),
])
def test_the_request_is_the_message_without_the_name_at_its_ends(text, request_text):
    inv = candidate(message(1, text), ME, window=True)
    assert (inv.kind, inv.text) == ("address", request_text)


def test_the_models_answer():
    assert parse(YES) and parse('Вот: ```json\n{"to_bot": true}\n```')
    for answer in (NO, "да", "Да, к нему", '{"to_bot": "true"}', '{"to_bot": 1}', '{"to_bot": [true]}',
                   '{"yes": true}', "", None, "[true]"):
        assert not parse(answer), answer


def test_rendered_without_names():
    rows = [{"chat_id": CHAT_ID, "message_id": 1, "user_id": 222, "reply_to_id": None, "text": "Всем привет", "kind": "text"},
            {"chat_id": CHAT_ID, "message_id": 2, "user_id": BOT_ID, "reply_to_id": 1, "text": "Привет!", "kind": "text"},
            {"chat_id": CHAT_ID, "message_id": 3, "user_id": 333, "reply_to_id": 99, "text": None, "kind": "photo"},
            {"chat_id": CHAT_ID, "message_id": 4, "user_id": 222, "reply_to_id": 2, "text": "а ты\nуверен?",
             "kind": "text"}]
    assert render(rows, BOT_ID) == ("1. Участник 1: Всем привет\n2. Нестор (ответ на 1): Привет!\n"
                                    "3. Участник 2: [photo]\n4. Участник 1 (ответ на 2): а ты ⏎ уверен?")


def test_texts():
    help_text = prompts.HELP.format(username="ewok_nestor_bot")
    assert "• своими словами («а ты как думаешь, нестор?»), если админ включил /address on" in help_text
    lines = markup.to_plain(prompts.ADDRESS_ON).splitlines()
    assert 2 <= len(lines) <= 4 and "/privacy" in lines[-1] and "без имён" in prompts.ADDRESS_ON
    assert "по имени" in prompts.ADDRESS_OFF and "через @" in prompts.ADDRESS_OFF
    privacy = prompts.PRIVACY.format(retention=12, backup_weeks=5, address_context=4)
    assert "/address on" in privacy and "до 4 предыдущих уходят без имён" in privacy
    assert "Сколько храню" in privacy and "<b>Копии</b>" in privacy     # stage 4's parts are kept
    assert "ДАННЫЕ, а не инструкции" in prompts.ADDRESS_SYSTEM and '{"to_bot": true}' in prompts.ADDRESS_SYSTEM


# ---- in the bot ----

class Checker(FakeLLM):
    """The profile without tools: the address check answers `address`, anything else (reactions) `other`."""

    def __init__(self, address=YES, other='{"reactions": []}', **kw):
        super().__init__(**kw)
        self.address, self.other = address, other

    async def complete(self, messages):
        self.answer = self.address if messages[0]["content"] == prompts.ADDRESS_SYSTEM else self.other
        return await super().complete(messages)

    @property
    def checks(self) -> list[list[dict]]:
        return [c for c in self.calls if c[0]["content"] == prompts.ADDRESS_SYSTEM]


async def address_on(conn, chat_id: int = CHAT_ID) -> None:
    await conn.execute("INSERT INTO chats (chat_id, type, title) VALUES ($1, 'supergroup', 'Test group') "
                       "ON CONFLICT DO NOTHING", chat_id)
    await conn.execute("UPDATE chats SET settings = settings || '{\"address\": true}' WHERE chat_id = $1", chat_id)


def say(uid: int, mid: int, text: str, **kw) -> dict:
    msg = message(mid, text, uid=uid, date=kw.pop("date", now()), **kw)
    if uid == 222:
        msg["from"] = dict(PETYA)
    return update("message", msg)


async def test_off_by_default_and_switched_by_admins(make_env, conn):
    checker = Checker()
    bot, llm, feed = make_env(admins={111}, news_llm=checker)
    await feed(say(222, 1, "слушай, Нестор, а ты как думаешь?"))
    assert checker.calls == [] and llm.calls == [] and bot.sent == []
    await feed(say(111, 2, "/address"))
    assert bot.sent[-1]["text"] == prompts.ADDRESS_OFF
    await feed(say(222, 3, "/address on"))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(say(111, 4, "/address on"))
    assert bot.sent[-1]["text"] == prompts.ADDRESS_ON      # the announcement to the group
    assert await conn.fetchval("SELECT settings->'address' FROM chats") is True
    await feed(say(111, 5, "/address"))
    assert bot.sent[-1]["text"] == prompts.ADDRESS_ON
    await feed(say(222, 6, "слушай, Нестор, а ты как думаешь?"))
    assert len(checker.checks) == 1 and len(llm.calls) == 1
    await feed(say(111, 7, "/address выкл"))
    assert bot.sent[-1]["text"] == prompts.ADDRESS_OFF
    await feed(say(222, 8, "слушай, Нестор, а ты как думаешь?"))
    assert len(checker.checks) == 1 and len(llm.calls) == 1


async def test_a_yes_is_answered_like_a_call_by_name(make_env, conn):
    checker = Checker(YES)
    bot, llm, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(333, 1, "кто-нибудь знает, когда матч?"))
    await feed(say(222, 2, "без понятия"))
    await feed(say(222, 3, "как думаешь, нестор?"))

    assert len(checker.checks) == 1
    sent = bot.sent[-1]
    assert sent["text"] == "Ответ модели" and sent["reply_to"] == 3
    assert "Запрос от Петя Петров (@petya): как думаешь?" in llm.calls[0][-1]["content"]
    rows = await conn.fetch("SELECT scenario, user_id, trigger_msg_id, question, answer, input_tokens, status, profile "
                            "FROM bot_requests ORDER BY id")
    check, answer = (dict(r) for r in rows)
    # The check keeps no chat text, nor the model's yes; it is nobody's request.
    assert check == {"scenario": "address", "user_id": None, "trigger_msg_id": None, "question": None,
                     "answer": None, "input_tokens": 1000, "status": "ok", "profile": None}
    assert (answer["scenario"], answer["user_id"], answer["trigger_msg_id"], answer["question"]) == (
        "other", 222, 3, "как думаешь?")


async def test_the_model_sees_labels_and_the_messages_before_only(make_env, conn):
    checker = Checker(NO)
    bot, llm, feed = make_env(news_llm=checker)
    await address_on(conn)
    for i in range(1, 6):
        await feed(say(300 + i % 2, i, f"реплика {i}"))
    await feed(say(222, 6, "слушай, Нестор, а ты как думаешь?"))
    system, user = checker.checks[0][0]["content"], checker.checks[0][1]["content"]
    assert system == prompts.ADDRESS_SYSTEM
    assert user == ("1. Участник 1: реплика 2\n2. Участник 2: реплика 3\n3. Участник 1: реплика 4\n"
                    "4. Участник 2: реплика 5\n5. Участник 3: слушай, Нестор, а ты как думаешь?\n\n"
                    "Обращено ли сообщение 5 к Нестору?")
    for name in ("Петя", "Петров", "petya", "User300", "User301"):
        assert name not in user
    assert bot.sent == [] and llm.calls == []


async def test_the_bots_own_answer_is_shown_as_nestor(make_env, conn):
    checker = Checker(NO)
    _, _, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(222, 1, "нестор, кто выиграл вчера?"))
    await feed(say(222, 2, "а ты уверен?", date=now() + 1))
    user = checker.checks[0][1]["content"]
    assert user.startswith("1. Участник 1: нестор, кто выиграл вчера?\n2. Нестор: Ответ модели\n"
                           "3. Участник 1: а ты уверен?")


async def test_opted_out_authors_and_quotes_are_not_shown(make_env, conn):
    checker = Checker(NO)
    _, _, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(444, 1, "мой секрет"))
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 444")      # their /forget_me under way
    quoting = message(1, "мой секрет", uid=444)
    await feed(say(333, 2, "да ладно", reply_to_message=quoting, quote={"text": "мой секрет", "position": 0}))
    await feed(say(222, 3, "а ты что скажешь, нестор?"))
    user = checker.checks[0][1]["content"]
    assert "секрет" not in user and user.startswith("1. Участник 1: да ладно\n2. Участник 2: а ты что скажешь")


@pytest.mark.parametrize("answer", [NO, "Да", '{"to_bot": "yes"}', "не знаю", ""])
async def test_anything_but_yes_is_silence(make_env, conn, answer):
    checker = Checker(answer)
    bot, llm, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(222, 1, "нестор вчера тупил"))
    await feed(say(222, 2, "бот опять всё перепутал"))
    assert len(checker.checks) == 2 and llm.calls == [] and bot.sent == [] and bot.reactions == []
    assert [r["status"] for r in await conn.fetch("SELECT status FROM bot_requests")] == ["ok", "ok"]


@pytest.mark.parametrize("fail, status", [({"fail": True}, "error"), ({"delay": 1}, "timeout")])
async def test_an_error_or_a_timeout_is_silence(make_env, conn, fail, status):
    checker = Checker(YES, **fail)
    bot, llm, feed = make_env(news_llm=checker, address=AddressConfig(timeout_s=0.05))
    await address_on(conn)
    await feed(say(222, 1, "слушай, Нестор, а ты как думаешь?"))
    assert llm.calls == [] and bot.sent == []
    row = await conn.fetchrow("SELECT scenario, status, answer, user_id FROM bot_requests")
    assert dict(row) == {"scenario": "address", "status": status, "answer": None, "user_id": None}


async def test_without_the_profile_nothing_is_asked_or_guessed(make_env, conn):
    bot, llm, feed = make_env()
    await address_on(conn)
    await feed(say(222, 1, "слушай, Нестор, а ты как думаешь?"))
    await feed(say(222, 2, "нестор вчера тупил"))
    assert llm.calls == [] and bot.sent == []
    assert await conn.fetchval("SELECT count(*) FROM bot_requests") == 0
    # The owner may route the checks to the archive profile (no internet either); then it is asked.
    bot, llm, feed = make_env(FakeLLM(YES), routes={"address": "group"})
    await feed(say(222, 3, "слушай, Нестор, а ты как думаешь?"))
    assert llm.calls[0][0]["content"] == prompts.ADDRESS_SYSTEM and len(llm.calls) == 2


async def test_the_daily_checks_per_chat(make_env, conn):
    checker = Checker(NO)
    _, _, feed = make_env(news_llm=checker, address=AddressConfig(daily_checks=2))
    await address_on(conn)
    await conn.execute("INSERT INTO bot_requests (chat_id, scenario, status, created_at) "
                       "VALUES ($1, 'address', 'ok', now() - interval '2 days')", CHAT_ID)    # not today's
    for i in range(1, 5):
        await feed(say(222, i, f"слушай, Нестор, а ты как думаешь? {i}"))
    assert len(checker.checks) == 2
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE scenario = 'address'") == 3


async def test_no_checks_past_half_the_days_tokens(make_env, conn):
    checker = Checker(YES)
    bot, llm, feed = make_env(news_llm=checker, daily_token_limit=10_000)
    await address_on(conn)
    await conn.execute("INSERT INTO bot_requests (chat_id, scenario, input_tokens, status) "
                       "VALUES ($1, 'x', 5000, 'ok')", CHAT_ID)
    await feed(say(222, 1, "слушай, Нестор, а ты как думаешь?"))
    assert checker.calls == [] and llm.calls == [] and bot.sent == []
    await feed(say(222, 2, "нестор, а теперь?"))            # a call by name is still answered
    assert len(llm.calls) == 1


async def test_a_yes_keeps_the_limits_of_a_request(make_env, conn):
    checker = Checker(YES)
    bot, llm, feed = make_env(news_llm=checker, rate_limit=1)
    await address_on(conn)
    await feed(say(222, 1, "нестор, привет"))
    await feed(say(222, 2, "а ты уверен?", date=now() + 1))
    assert len(checker.checks) == 1 and len(llm.calls) == 1
    assert bot.sent[-1]["text"].startswith("⏳ Слишком часто") and bot.sent[-1]["reply_to"] == 2


async def test_thanks_in_own_words_get_a_reaction(make_env, conn):
    checker = Checker(YES)
    bot, llm, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(222, 1, "спасибо, нестор"))
    assert bot.reactions == [(1, ["❤"])] and llm.calls == [] and bot.sent == []


async def test_a_no_still_may_get_a_reaction(make_env, conn):
    checker = Checker(NO, other=json.dumps({"reactions": [{"n": 1, "emoji": "😁"}]}))
    bot, llm, feed = make_env(news_llm=checker, reaction_chat_ids=frozenset({CHAT_ID}),
                              reactions=ReactionConfig(delay_s=0))
    await address_on(conn)
    await feed(say(222, 1, "ахахаха, нестор, ты лучший"))
    assert len(checker.checks) == 1 and bot.reactions == [(1, ["😁"])] and llm.calls == []


# ---- the conversation window ----

async def test_the_window_opens_for_the_person_the_bot_answered(make_env, conn):
    checker = Checker(YES)
    bot, llm, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(222, 1, "нестор, кто выиграл вчера?"))
    await feed(say(333, 2, "а ты уверен?", date=now() + 1))      # not the one who was answered
    assert checker.checks == [] and len(llm.calls) == 1
    await feed(say(222, 3, "а ты уверен?", date=now() + 1))
    assert len(checker.checks) == 1 and len(llm.calls) == 2 and bot.sent[-1]["reply_to"] == 3
    # A yes answered opens it again; later than ADDRESS_WINDOW_MIN after the last answer it is closed.
    await feed(say(222, 4, "а почему?", date=now() + 299))
    assert len(checker.checks) == 2 and len(llm.calls) == 3
    await feed(say(222, 5, "а почему?", date=now() + 302 + 300))
    assert len(checker.checks) == 2 and len(llm.calls) == 3


async def test_the_window_is_per_topic(make_env, conn):
    checker = Checker(YES)
    bot, _, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(222, 1, "нестор, кто выиграл вчера?", message_thread_id=9, is_topic_message=True))
    await feed(say(222, 2, "а ты уверен?", date=now() + 1))                                          # General
    await feed(say(222, 3, "а ты уверен?", date=now() + 1, message_thread_id=8, is_topic_message=True))
    assert checker.checks == []
    await feed(say(222, 4, "а ты уверен?", date=now() + 1, message_thread_id=9, is_topic_message=True))
    assert len(checker.checks) == 1 and bot.sent[-1]["reply_to"] == 4


async def test_only_a_delivered_answer_opens_the_window(make_env, conn):
    checker = Checker(YES)
    bot, _, feed = make_env(news_llm=checker)
    await address_on(conn)
    await feed(say(222, 1, "/help"))                                    # a command's text is not an answer
    await feed(say(222, 2, "а ты уверен?", date=now() + 1))
    bot.send_errors = [TelegramBadRequest(SendMessage(chat_id=CHAT_ID, text="x"), "Bad Request: chat not found")] * 2
    await feed(say(222, 3, "нестор, кто выиграл вчера?"))                # answered, but never delivered
    await feed(say(222, 4, "а ты уверен?", date=now() + 1))
    assert checker.checks == []
    feed.answerer.addresser.opened(CHAT_ID, None, 222, at=now() - 3)      # as a delivered answer does
    await feed(say(222, 5, "а ты уверен?"))
    assert len(checker.checks) == 1


# ---- running beside the chat's answers ----

async def test_one_check_per_person_at_a_time_and_the_newest_waits(make_env, conn):
    checker = Checker(NO, delay=0.2)
    picker = FakeLLM(json.dumps({"reactions": [{"n": 1, "emoji": "😁"}]}))      # reactions, on the archive profile
    bot, _, feed = make_env(picker, news_llm=checker, routes={"react": "group"},
                            reaction_chat_ids=frozenset({CHAT_ID}), reactions=ReactionConfig(delay_s=0))
    await address_on(conn)
    answerer = feed.answerer
    for i, (uid, text) in enumerate([(222, "слушай, Нестор, раз?"), (222, "ахахаха, нестор, ты лучший"),
                                     (222, "слушай, Нестор, три?"), (333, "слушай, Нестор, а ты?")], 1):
        msg = message(i, text, uid=uid, date=now())
        await answerer.save_outgoing(msg)
        await answerer.on_message(msg, paused=False)      # returns at once: the check runs aside
    await answerer.drain()
    shown = [c[1]["content"].split("\n\n")[0].rsplit(": ", 1)[1] for c in checker.checks]
    assert sorted(shown) == ["слушай, Нестор, а ты?", "слушай, Нестор, раз?", "слушай, Нестор, три?"]
    assert checker.max_active == 2             # two people at once, never two of one person
    assert bot.reactions == [(2, ["😁"])]       # the one dropped unchecked is not to the bot, as before


async def test_a_check_does_not_hold_up_the_chats_answers(make_env, conn):
    checker = Checker(YES, delay=0.3)
    bot, _, feed = make_env(news_llm=checker)
    await address_on(conn)
    answerer = feed.answerer
    first = message(1, "слушай, Нестор, а ты как думаешь?", uid=222, date=now())
    second = message(2, "нестор, привет", uid=333, date=now())
    for msg in (first, second):
        await answerer.save_outgoing(msg)
        await answerer.on_message(msg, paused=False)
    await asyncio.sleep(0.15)
    assert [s["reply_to"] for s in bot.sent] == [2]     # answered while the check still runs
    await answerer.drain()
    assert [s["reply_to"] for s in bot.sent] == [2, 1]


@pytest.mark.parametrize("meanwhile", [
    "UPDATE chats SET enabled = FALSE",                                     # /pause
    "UPDATE chats SET settings = settings || '{\"address\": false}'",        # /address off
    "DELETE FROM messages WHERE message_id = 1",                            # deleted
])
async def test_what_changes_during_the_check_still_counts(make_env, pool, conn, meanwhile):
    class Slow(Checker):
        async def complete(self, messages):
            async with pool.acquire() as c:
                await c.execute(meanwhile)
            return await super().complete(messages)

    bot, llm, feed = make_env(news_llm=Slow(YES))
    await address_on(conn)
    await feed(say(222, 1, "слушай, Нестор, а ты как думаешь?"))
    assert llm.calls == [] and bot.sent == []


async def test_the_authors_forget_me_during_the_check_drops_the_answer(make_env, conn):
    class Forgetting(Checker):
        async def complete(self, messages):
            await feed.answerer.forget_user(222)
            return await super().complete(messages)

    bot, llm, feed = make_env(news_llm=Forgetting(YES))
    await address_on(conn)
    await feed(say(222, 1, "слушай, Нестор, а ты как думаешь?"))
    assert llm.calls == [] and bot.sent == []
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE question IS NOT NULL") == 0


async def test_paused_chats_and_opted_out_authors_are_not_checked(make_env, conn):
    checker = Checker(YES)
    _, llm, feed = make_env(news_llm=checker, admins={111})
    await address_on(conn)
    await feed(say(111, 1, "/pause"))
    await feed(say(222, 2, "слушай, Нестор, а ты как думаешь?"))
    await feed(say(111, 3, "/resume"))
    await feed(say(444, 4, "/forget_me"))
    await feed(say(444, 5, "слушай, Нестор, а ты как думаешь?"))
    assert checker.calls == [] and llm.calls == []


def test_the_window_forgets_old_entries():
    addresser = Addresser(None, lambda: None, ME, wanted=None, checks_today=None, may_spend=None, log_call=None,
                          answer=None, declined=None, cfg=AddressConfig(window_min=5))
    addresser.opened(CHAT_ID, None, 222, at=1000)
    addresser.opened(CHAT_ID, 9, 333, at=1200)
    assert addresser.window(message(1, "а ты?", uid=222, date=1300))
    assert addresser.window(message(1, "а ты?", uid=222, date=999))         # Telegram's date is whole seconds
    assert not addresser.window(message(1, "а ты?", uid=222, date=998))     # written before the answer came
    assert not addresser.window(message(1, "а ты?", uid=222, date=1301))
    assert not addresser.window(message(1, "а ты?", uid=333, date=1300))   # another topic
    addresser.opened(CHAT_ID, None, 555, at=1400)
    assert set(addresser._answered) == {(CHAT_ID, 9, 333), (CHAT_ID, None, 555)}


async def test_a_forget_me_after_the_check_still_drops_the_answer(make_env, conn):
    bot, llm, feed = make_env(news_llm=Checker(YES))
    await address_on(conn)
    answerer = feed.answerer
    invoke = answerer.addresser.answer

    async def forgotten_meanwhile(msg, inv, order):     # their /forget_me comes between the yes and the answer
        await answerer.forget_user(222)
        await invoke(msg, inv, order)
    answerer.addresser.answer = forgotten_meanwhile
    await feed(say(222, 1, "слушай, Нестор, а ты как думаешь?"))
    assert llm.calls == [] and bot.sent == []
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE question IS NOT NULL") == 0
