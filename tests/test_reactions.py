"""Reactions to emotional messages: the code's candidates, the model's picks, and the limits."""
import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMessageReaction
from aiogram.types import ReactionTypeEmoji

from nestor.bot import markup, prompts, reactions
from nestor.bot.reactions import (
    ALLOWED,
    STANDARD,
    Guess,
    ReactionConfig,
    acknowledgement,
    eligible,
    guess,
    parse,
    thanks,
)

from .factories import CHAT_ID, message, update
from .test_answerer import FakeLLM, make_env_fixture  # noqa: F401 - the fixture is used by name

NOW = lambda: int(datetime.now(timezone.utc).timestamp())  # noqa: E731
FAST = ReactionConfig(delay_s=0)


def answer(*pairs) -> str:
    return json.dumps({"reactions": [{"n": n, "emoji": e} for n, e in pairs]}, ensure_ascii=False)


def reactions_text(bot) -> str:
    return markup.to_plain(bot.sent[-1]["text"])


def picks(*pairs) -> FakeLLM:
    return FakeLLM(answer(*pairs))


class SeqLLM(FakeLLM):
    """One answer per call, in order."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    async def complete(self, messages):
        self.answer = self.answers.pop(0)
        return await super().complete(messages)


# ---- pure parts ----

def test_the_bot_uses_only_standard_and_friendly_reactions():
    assert set(ALLOWED) <= STANDARD and len(STANDARD) == 73
    assert not {"👎", "🤡", "💩", "🖕", "🤬", "🤮", "💊", "😡", "🥱", "💋"} & set(ALLOWED)


@pytest.mark.parametrize("text, expected", [
    ("Ахахахах, ну ты даёшь", Guess("🤣", True)),
    ("лол", Guess("🤣", True)),
    ("ну да)))", Guess("😁", True)),
    ("Ура, сдал на права!", Guess("🎉", True)),
    ("С днём рождения!", Guess("🎉", True)),
    ("обожаю этот трек ❤️", Guess("❤", True)),
    ("жаль, что не смогу", Guess("😢", False)),           # the model decides: maybe just politeness
    ("у меня комп умер", Guess("🙏", False)),
    ("ничего себе новости", Guess("😱", False)),
    ("Это вообще как?!?!", Guess(None, False)),
    ("привет, как дела", None),
    ("", None),
    (None, None),
    # Not laughter or love, whatever letters they share with it.
    ("Соседи отравились угарным газом, в реанимации", None),
    ("Поговорим о страхах", None), ("в рубахах пришли", None), ("её хахаль ушёл", None),
    ("ха-ха, смешно", Guess("🤣", True)), ("хехе", Guess("🤣", True)), ("haha", Guess("🤣", True)),
    ("Я на детей постоянно ору, не знаю что делать", Guess("🤣", False)),   # the model reads it
    ("Я не люблю, когда так делают", Guess("❤", False)),
    ("Поздравляю с победой!", Guess("🎉", True)),
    ("Не поздравляйте меня в этом году, не до праздников", Guess("🎉", False)),   # the model reads it
    ("С др. стороны, это опасно", Guess("🎉", False)),
    ("Надо поздравлять Петю?", Guess("🎉", False)),
    ("С др!", Guess("🎉", False)),
])
def test_guess(text, expected):
    assert guess(text) == expected


def test_eligible():
    assert eligible(message(1, "ахаха"))
    assert not eligible(message(1, None))
    assert not eligible(message(1, "/stats ахаха"))
    assert not eligible(message(1, "ахаха", **{"from": {"id": 5, "is_bot": True, "first_name": "B"}}))
    assert not eligible(message(1, "ахаха", forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "X"}))
    assert not eligible(message(1, "ахаха", sender_chat={"id": CHAT_ID, "type": "supergroup"}))
    assert eligible(message(1, None, caption="ахаха", photo=[]))


@pytest.mark.parametrize("text, emoji", [
    ("спасибо!", "❤"), ("Спасибо большое, нестор 🙏", "❤"), ("спс", "❤"), ("благодарю", "❤"), ("Thanks", "❤"),
    ("ок)", "👌"), ("Понял.", "👌"), ("ясно", "👌"),
    # «ок» and thanks together, with or without commas, and the thanks of a compliment.
    ("а понял спасибо", "❤"), ("ок спс", "❤"), ("Ясно, спасибо!", "❤"), ("точно, спасибо", "❤"),
    ("спасибо, ты лучший", "❤"), ("ты лучший", "❤"), ("и тебе того же", "❤"), ("а, ясно, понял", "👌"),
    ("спасибочки", "❤"), ("Благодарен!", "❤"),
    ("а круто", None), ("ок, спасибо, а завтра?", None), ("понял, спасибо, но ты не прав", None),
    ("👍", "👍"), ("❤️", "❤"), ("🔥🔥", "🔥"), (")))", "😁"), ("🙂", "👌"),
    ("❓", None), ("⁉️", None), ("？", None), ("👎", None), ("🤬", None), ("!!!", None), ("🍺", None),
    ("понял?", None), ("спасибо, а почему так?", None), ("ок, а завтра?", None), ("что?", None), ("", None),
    ("??", None),
])
def test_acknowledgement(text, emoji):
    assert acknowledgement(text) == emoji


@pytest.mark.parametrize("text, expected", [
    # A thanks among other words that ask nothing.
    ("о, спасибо", True), ("супер, спасибо!", True), ("круто, спасибо", True), ("класс, спасибо!", True),
    ("отлично, спасибо", True), ("спасибо, выручил", True), ("спасибо, очень помог", True),
    ("спасибо, интересно", True), ("спасибо, понял", True), ("спасибочки", True), ("огромное тебе спасибо!", True),
    ("огромное тебе спасибо, ты лучший", True), ("спасибо тебе за помощь", True), ("а, супер, спасибо", True),
    ("спасибо, нестор, выручил", True), ("спасибо", True), ("мерси 🙏", True), ("ещё раз спасибо!", True),
    ("спасибо за ответ", True), ("ты лучший, спасибо", True), ("о, супер, нестор, спасибо", True),
    # Anything else may ask more: a question, «ты», a request, an «а», «и», «но», any word not of a thanks.
    ("спасибо, а на завтра", False), ("спасибо, но это не так", False), ("спасибо, и про Цоя тоже", False),
    ("спасибо, расскажи ещё", False), ("спасибо, ты уверен?", False), ("спасибо! а почему так?", False),
    ("понял, спасибо, но ты не прав", False), ("спасибо, что ещё послушать", False), ("спасибо?", False),
    ("спасибо, посоветуй ещё что-нибудь похожее", False), ("спасибо! переведи это на английский", False),
    ("спасибо, сделай покороче", False), ("спасибо, подробнее", False), ("спасибо! теперь про Наутилус", False),
    ("посоветуй фильм на вечер, заранее спасибо", False), ("переведи на английский, нестор, спасибо", False),
    ("спасибо, теперь переведи на английский", False), ("спасибо, давай подробнее", False),
    ("спасибо, только там ошибка: это было в 2019", False), ("как это сделать, спасибо", False),
    ("спасибо за ответ, теперь покажи код", False),
    # No thanks at all: a praise, «того же», «благодаря».
    ("ну ты молодец, выручил", False), ("а какой лучший фильм", False), ("а того же автора посоветуешь", False),
    ("благодаря дождю матч отменили", False), ("ок", False), ("🙏", False), ("", False), (None, False),
])
def test_thanks(text, expected):
    assert thanks(text) is expected


def test_parse():
    allowed = frozenset({"😁", "❤", "🎉"})
    answer = 'Вот: {"reactions": [{"n": 1, "emoji": "😁"}, {"n": 2, "emoji": "❤️"}, {"n": 1, "emoji": "🎉"}, ' \
             '{"n": 3, "emoji": "👎"}, {"n": 9, "emoji": "🎉"}, {"n": true, "emoji": "🎉"}, ' \
             '{"n": "4", "emoji": "🎉"}, ' \
             '"мусор", {"n": 4}]}'
    assert parse(answer, 4, allowed) == {1: "😁", 2: "❤"}
    assert parse("не JSON", 4, allowed) == {} and parse('{"reactions": "нет"}', 4, allowed) == {}
    assert parse(None, 4, allowed) == {}


# ---- in the bot ----

async def test_the_model_picks_reactions_without_seeing_names(make_env, conn):
    model = SeqLLM(answer((1, "😁"), (2, "🎉")), answer((1, "👎")))
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    await feed(update("message", message(1, "Ахахаха, Петя опять проспал", uid=222, date=NOW())))
    await feed(update("message", message(2, "ну всё, приехали!!!", uid=333, date=NOW())))
    await feed(update("message", message(3, "встречаемся в шесть", uid=444, date=NOW())))

    # n=2 of the first batch does not exist; 👎 is not the bot's to put; a neutral message is not asked about.
    assert bot.reactions == [(1, ["😁"])]
    assert llm.calls == []                                 # the archive model is not involved
    system, user = model.calls[0][0]["content"], model.calls[0][1]["content"]
    assert user == "1. Ахахаха, Петя опять проспал" and "User222" not in user
    assert "👎" not in system and "😁" in system
    assert len(model.calls) == 2 and model.calls[1][1]["content"] == "1. ну всё, приехали!!!"
    rows = await conn.fetch("SELECT * FROM bot_requests WHERE scenario = 'reaction'")
    logged = [(r["chat_id"], r["user_id"], r["input_tokens"], r["status"]) for r in rows]
    assert logged == [(CHAT_ID, None, 1000, "ok")] * 2


async def test_without_the_model_only_sure_guesses(make_env):
    bot, llm, feed = make_env(reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    await feed(update("message", message(2, "жаль, конечно", uid=333, date=NOW())))
    await feed(update("message", message(3, "С днём рождения, Вася!", uid=444, date=NOW())))
    assert bot.reactions == [(1, ["🤣"]), (3, ["🎉"])] and llm.calls == []


async def test_a_failing_model_falls_back_to_sure_guesses(make_env):
    bot, llm, feed = make_env(news_llm=FakeLLM(fail=True), reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == [(1, ["🤣"])]


async def test_reactions_follow_what_the_chat_allows(make_env):
    model = picks((1, "😁"))
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    bot.available_reactions = [ReactionTypeEmoji(emoji="👍"), ReactionTypeEmoji(emoji="❤️")]
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == []                             # the model picked one the chat does not allow
    assert "Только эмодзи из этого списка: 👍 ❤\n" in model.calls[0][0]["content"]

    bot.available_reactions = []                           # reactions switched off in the group
    feed.answerer.reactor._available.clear()
    await feed(update("message", message(2, "ахахах", uid=222, date=NOW())))
    assert len(model.calls) == 1 and bot.reactions == []


async def test_a_refused_reaction_is_not_tried_again(make_env):
    bot, llm, feed = make_env(reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    bot.reaction_errors["🤣"] = TelegramBadRequest(SetMessageReaction(chat_id=CHAT_ID, message_id=1, reaction=[]),
                                                   "Bad Request: REACTION_INVALID")
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    assert "🤣" not in feed.answerer.reactor._available[CHAT_ID][1]
    del bot.reaction_errors["🤣"]
    await feed(update("message", message(2, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == [] and bot.get_chat_calls == 1


async def test_hourly_and_daily_limits(make_env, conn):
    model = picks((1, "😁"), (2, "🎉"))
    cfg = ReactionConfig(delay_s=0, per_hour=1, calls_per_day=1)
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}), reactions=cfg)
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == [(1, ["😁"])]
    assert "Не больше 1 реакций" in model.calls[0][0]["content"]
    await feed(update("message", message(2, "ура!", uid=333, date=NOW())))
    assert bot.reactions == [(1, ["😁"])] and len(model.calls) == 1   # an hour has not passed

    feed.answerer.reactor._done.clear()
    await feed(update("message", message(3, "ура!", uid=333, date=NOW())))
    assert len(model.calls) == 1 and bot.reactions[-1] == (3, ["🎉"])  # the day's model calls are used up


async def test_reactions_stop_asking_the_model_at_half_the_daily_tokens(make_env, conn):
    model = picks((1, "😁"))
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST,
                              daily_token_limit=10_000)
    await conn.execute(
        "INSERT INTO bot_requests (chat_id, scenario, input_tokens, status) VALUES ($1, 'x', 5000, 'ok')", CHAT_ID)
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    assert model.calls == [] and bot.reactions == [(1, ["🤣"])]


async def test_old_deleted_and_forgotten_messages_get_nothing(make_env, conn):
    model = picks((1, "😁"), (2, "😁"))
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW() - 3600)))
    assert model.calls == [] and bot.reactions == []

    # Queued, then the author used /forget_me before the batch was due.
    reactor = feed.answerer.reactor
    msg = message(2, "ахахах", uid=333, date=NOW())
    await feed.answerer.save_outgoing(msg)
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 333")
    assert reactor.consider(msg)
    await reactor.drain()
    assert model.calls == [] and bot.reactions == []


async def test_without_the_model_the_sure_guess_is_on_the_words_as_they_are_now(make_env, conn):
    bot, llm, feed = make_env(reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    reactor = feed.answerer.reactor
    for mid, edited in ((1, "жаль, конечно"), (2, "С днём рождения, Вася!")):
        msg = message(mid, "ахахах", uid=222, date=NOW())
        await feed.answerer.save_outgoing(msg)
        await conn.execute("UPDATE messages SET text = $1, edited_at = now() WHERE message_id = $2", edited, mid)
        assert reactor.consider(msg)                  # queued by the words that came
        await reactor.drain()
    assert bot.reactions == [(2, ["🎉"])] and llm.calls == []


async def test_a_message_edited_into_plain_words_is_not_shown_to_the_model(make_env, conn):
    model = picks((1, "😁"))
    bot, _, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}), reactions=FAST)
    msg = message(1, "ахахах", uid=222, date=NOW())
    await feed.answerer.save_outgoing(msg)
    await conn.execute("UPDATE messages SET text = 'встречаемся в шесть', edited_at = now() WHERE message_id = 1")
    assert feed.answerer.reactor.consider(msg)
    await feed.answerer.reactor.drain()
    assert model.calls == [] and bot.reactions == []


async def test_reactions_are_off_by_default_and_switched_by_admins(make_env):
    bot, llm, feed = make_env(admins={111}, reactions=FAST)
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == []
    await feed(update("message", message(2, "/reactions on", uid=222)))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(update("message", message(3, "/reactions on", uid=111)))
    assert reactions_text(bot).startswith("😄 Реакции включены.")
    await feed(update("message", message(4, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == [(4, ["🤣"])]

    await feed(update("message", message(5, "/pause", uid=111)))
    await feed(update("message", message(6, "ахахах", uid=222, date=NOW())))
    await feed(update("message", message(7, "/resume", uid=111)))
    await feed(update("message", message(8, "/reactions off", uid=111)))
    assert reactions_text(bot).startswith("😄 Реакции выключены.")
    await feed(update("message", message(9, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == [(4, ["🤣"])]


async def test_a_batch_waits_and_keeps_the_newest(make_env):
    model = picks((1, "😁"), (3, "🎉"))
    cfg = ReactionConfig(delay_s=0.2, batch=3)
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}), reactions=cfg)
    answerer = feed.answerer
    for i in range(1, 6):
        await answerer.save_outgoing(message(i, f"ахахах {i}", uid=200 + i, date=NOW()))
        await answerer.on_message(message(i, f"ахахах {i}", uid=200 + i, date=NOW()), paused=False)
    await answerer.drain()
    assert len(model.calls) == 1
    assert model.calls[0][1]["content"] == "1. ахахах 3\n2. ахахах 4\n3. ахахах 5"
    assert bot.reactions == [(3, ["😁"]), (5, ["🎉"])]


async def test_thanks_to_the_bot_gets_a_reaction_not_an_answer(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, привет", uid=222)))
    answer = bot.sent[-1]
    bot_msg = {"message_id": answer["message_id"], "date": NOW(), "chat": {"id": CHAT_ID, "type": "supergroup"},
               "from": {"id": 42, "is_bot": True, "first_name": "Нестор", "username": "ewok_nestor_bot"},
               "text": answer["text"]}
    await feed(update("message", message(2, "спасибо!", uid=222, reply_to_message=bot_msg)))
    await feed(update("message", message(3, "нестор, ок)", uid=222)))
    await feed(update("message", message(4, "спасибо, а почему?", uid=222, reply_to_message=bot_msg)))
    assert (2, ["❤"]) in bot.reactions and (3, ["👌"]) in bot.reactions
    assert len(llm.calls) == 2 and await conn.fetchval("SELECT count(*) FROM bot_requests") == 2


async def test_thanks_get_a_reaction_the_chat_allows_or_an_answer(make_env):
    bot, llm, feed = make_env()
    bot.available_reactions = [ReactionTypeEmoji(emoji="👍")]
    await feed(update("message", message(1, "нестор, спасибо!", uid=222)))
    assert bot.reactions == [(1, ["👍"])] and llm.calls == []
    bot.available_reactions = []                           # no reactions in this group: a normal answer
    feed.answerer.reactor._available.clear()
    await feed(update("message", message(2, "нестор, спасибо!", uid=222)))
    assert len(llm.calls) == 1 and bot.sent[-1]["reply_to"] == 2


async def test_a_yes_to_the_bots_question_or_to_someone_else_is_a_request(make_env):
    bot, llm, feed = make_env(FakeLLM("Тебе про Кино?"))
    await feed(update("message", message(1, "нестор, расскажи про группу", uid=222)))
    question = bot.sent[-1]
    bot_msg = {"message_id": question["message_id"], "date": NOW(), "chat": {"id": CHAT_ID, "type": "supergroup"},
               "from": {"id": 42, "is_bot": True, "first_name": "Нестор", "username": "ewok_nestor_bot"},
               "text": question["text"]}
    await feed(update("message", message(2, "угу", uid=222, reply_to_message=bot_msg)))
    assert len(llm.calls) == 2 and (2, ["👌"]) not in bot.reactions
    await feed(update("message", message(3, "нестор 👍", uid=222, reply_to_message=message(1, "x", uid=333))))
    assert len(llm.calls) == 3
    # The question need not be the last sentence of the bot's message.
    llm.answer = "Ты про «Группа крови» Кино? Напиши «да» или исполнителя."
    await feed(update("message", message(4, "нестор, расскажи про группу крови", uid=222)))
    bot_msg = bot_msg | {"message_id": bot.sent[-1]["message_id"], "text": bot.sent[-1]["text"]}
    await feed(update("message", message(5, "угу", uid=222, reply_to_message=bot_msg)))
    assert len(llm.calls) == 5 and (5, ["👌"]) not in bot.reactions


async def test_candidates_that_come_during_a_batch_get_the_next_one(make_env):
    model = SeqLLM(answer((1, "😁")), answer((1, "🎉")))
    model.delay = 0.3
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}),
                              reactions=ReactionConfig(delay_s=0.05))
    answerer = feed.answerer
    for i in (1, 2):
        msg = message(i, "ахахах", uid=200 + i, date=NOW())
        await answerer.save_outgoing(msg)
        await answerer.on_message(msg, paused=False)
        await asyncio.sleep(0.15)                          # the second comes while the first is with the model
    await answerer.drain()
    assert bot.reactions == [(1, ["😁"]), (2, ["🎉"])]


async def test_failed_model_calls_count_toward_the_day(make_env, conn):
    model = FakeLLM(fail=True)
    bot, llm, feed = make_env(news_llm=model, reaction_chat_ids=frozenset({CHAT_ID}),
                              reactions=ReactionConfig(delay_s=0, calls_per_day=1))
    for i in (1, 2, 3):
        await feed(update("message", message(i, "ну всё, приехали!!!", uid=222, date=NOW())))
    assert len(model.calls) == 1
    assert await conn.fetchval("SELECT status FROM bot_requests WHERE scenario = 'reaction'") == "error"


@pytest.mark.parametrize("meanwhile", [
    "UPDATE chats SET enabled = FALSE",                                      # /pause
    "UPDATE chats SET settings = settings || '{\"reactions\": false}'",       # /reactions off
    "UPDATE users SET opted_out = TRUE WHERE user_id = 222",                 # the author's /forget_me
    "UPDATE messages SET text = 'жаль, конечно', edited_at = now() WHERE message_id = 1",   # edited
])
async def test_what_changes_during_the_model_call_still_counts(make_env, pool, meanwhile):
    class Slow(FakeLLM):
        async def complete(self, messages):
            async with pool.acquire() as c:
                await c.execute(meanwhile)
            return await super().complete(messages)

    bot, llm, feed = make_env(news_llm=Slow(answer((1, "😁"))), reaction_chat_ids=frozenset({CHAT_ID}),
                              reactions=FAST)
    await feed(update("message", message(1, "ахахах", uid=222, date=NOW())))
    assert bot.reactions == []


def test_rules_are_quick():
    # Every group message goes through the rules. A long ordinary one costs under a millisecond (these 2000 take
    # about 1.5 s on a laptop), and a text made to make a rule backtrack no more than a few: one that does takes
    # seconds. The limits leave room for a slower machine, such as CI's.
    started = time.monotonic()
    for _ in range(2000):
        guess("обычное длинное сообщение без эмоций " * 20)
    assert time.monotonic() - started < 6
    for text in ("х" * 4000 + "ь", "ха" * 2000 + "ль", "ха " * 1300 + "ь"):
        started = time.monotonic()
        guess(text)
        assert time.monotonic() - started < 0.5, text[:10]
    assert reactions.normalize("❤️") == "❤"
