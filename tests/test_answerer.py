"""Stage 3 through the real dispatcher with a fake Telegram and a fake LLM."""
import asyncio
import itertools
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import Dispatcher
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Message, Update

from nestor import forget, retention
from nestor.bot import prompts
from nestor.bot.answerer import AnswerConfig, Answerer
from nestor.bot.llm import Completion
from nestor.bot.triggers import BotIdentity
from nestor.collector.app import Collector
from nestor.config import Settings
from nestor.mcp import tools

from .factories import CHAT_ID, message, update
from .test_collector import FakeBot as BaseFakeBot

BOT_ID = 42
_ids = itertools.count(10_000)


class FakeBot(BaseFakeBot):
    def __init__(self, admins=(), no_private=()):
        super().__init__()
        self.sent: list[dict] = []
        self.documents: list[dict] = []
        self.deleted: list[int] = []
        self.reactions: list = []
        self.actions: list = []
        self.admins = set(admins)
        self.no_private = set(no_private)  # users who never opened a private chat with the bot
        self.bot_status = ChatMemberStatus.MEMBER
        self.send_errors: list[Exception] = []  # raised by the next send_message calls, one per call
        self.reaction_errors: dict[str, Exception] = {}  # emoji -> raised by set_message_reaction
        self.available_reactions = None   # ChatFullInfo.available_reactions; None: all allowed
        self.get_chat_calls = 0

    async def send_message(self, chat_id, text, reply_parameters=None, **kw):
        if self.send_errors:
            raise self.send_errors.pop(0)
        mid = next(_ids)
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": mid,
                          "reply_to": reply_parameters.message_id if reply_parameters else None, **kw})
        return Message.model_validate({
            "message_id": mid, "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": chat_id, "type": "supergroup" if chat_id < 0 else "private", "title": "Test group"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Нестор", "username": "ewok_nestor_bot"},
            "text": text,
            # Telegram echoes the options back when the text has a link; aiogram adds Default(...) placeholders.
            **({"link_preview_options": {"is_disabled": True}} if "https://" in text else {}),
        })

    async def send_document(self, chat_id, document, caption=None, **kw):
        if chat_id in self.no_private:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot can't initiate conversation with a user")
        self.documents.append({"chat_id": chat_id, "data": document.data, "filename": document.filename,
                               "caption": caption})
        return True

    async def set_message_reaction(self, chat_id, message_id, reaction=None, **kw):
        emojis = [r.emoji for r in reaction or []]
        for emoji in emojis:
            if emoji in self.reaction_errors:
                raise self.reaction_errors[emoji]
        self.reactions.append((message_id, emojis))
        return True

    async def get_chat(self, chat_id, **kw):
        self.get_chat_calls += 1

        class Chat:
            pass
        chat = Chat()
        chat.id, chat.available_reactions = chat_id, self.available_reactions
        return chat

    async def send_chat_action(self, chat_id, action, **kw):
        self.actions.append(action)
        return True

    async def delete_message(self, chat_id, message_id, **kw):
        self.deleted.append(message_id)
        return True

    async def get_chat_member(self, chat_id, user_id, **kw):
        status = (self.bot_status if user_id == BOT_ID
                  else ChatMemberStatus.ADMINISTRATOR if user_id in self.admins else ChatMemberStatus.MEMBER)

        class M:
            pass
        M.status = status
        return M()


class FakeLLM:
    def __init__(self, answer="Ответ модели", delay=0.0, fail=False):
        self.calls: list[list[dict]] = []
        self.answer, self.delay, self.fail = answer, delay, fail
        self.active = self.max_active = 0

    async def complete(self, messages):
        self.calls.append(messages)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("hermes down")
            return Completion(self.answer, "claude-test", 1000, 200)
        finally:
            self.active -= 1


class FakeChecker:
    """music.Checker without the network: which videos and tracks "exist"."""

    def __init__(self, videos=None, spotify=None, apple=None):
        self.videos = videos or {}          # video id -> title; missing ids do not exist or cannot be embedded
        self.spotify_tracks = spotify or {}  # track id -> title
        self.apple_url = apple
        self.calls: list = []

    async def youtube(self, video_id):
        self.calls.append(("youtube", video_id))
        return {"title": self.videos[video_id]} if video_id in self.videos else None

    async def spotify(self, track_id):
        self.calls.append(("spotify", track_id))
        return {"title": self.spotify_tracks[track_id]} if track_id in self.spotify_tracks else None

    async def apple(self, artist, title):
        self.calls.append(("apple", artist, title))
        return self.apple_url


@pytest.fixture(name="make_env")
def make_env_fixture(pool):
    def make(llm=None, admins=(), web=None, smart=None, checker=None, no_private=(), deep=None, news_llm=None,
             routes=None, **cfg):
        bot, llm = FakeBot(admins, no_private), llm or FakeLLM()
        settings = Settings(bot_token="42:TEST", database_url="", allowed_chat_ids=frozenset({CHAT_ID}))
        collector = Collector(settings, pool)
        cfg.setdefault("allowed_chat_ids", frozenset({CHAT_ID}))
        collector.answerer = Answerer(bot, pool, llm, BotIdentity(BOT_ID, "ewok_nestor_bot", ("нестор", "бот")),
                                      lambda m: collector.store(m, is_edit=False), AnswerConfig(**cfg), web_llm=web,
                                      smart_llm=smart, checker=checker or FakeChecker(), deep_llm=deep,
                                      news_llm=news_llm, routes=routes)
        dp = Dispatcher()
        dp.include_router(collector.router())

        async def feed(raw):
            await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))
            await collector.answerer.drain()
        feed.answerer = collector.answerer
        return bot, llm, feed
    return make


async def test_keyword_invocation_answers_and_archives(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "Петя: налоги надо снизить", uid=222)))
    await feed(update("message", message(2, "нестор, оцени тезисы", uid=111)))

    assert [s["text"] for s in bot.sent] == ["Ответ модели"] and bot.sent[0]["reply_to"] == 2
    assert bot.reactions == [(2, ["👀"]), (2, [])]
    system, user = llm.calls[0][0]["content"], llm.calls[0][-1]["content"]
    ticket = system.split('ticket="')[1].split('"')[0]
    assert len(ticket) >= 20 and "налоги надо снизить" in user and "Сценарий: theses" in user
    assert await conn.fetchval("SELECT count(*) FROM request_tickets") == 0

    req = await conn.fetchrow("SELECT * FROM bot_requests")
    assert (req["status"], req["scenario"], req["input_tokens"], req["output_tokens"]) == ("ok", "theses", 1000, 200)
    assert req["answer_msg_ids"] == [bot.sent[0]["message_id"]]
    stored = await conn.fetchrow("SELECT user_id, text FROM messages WHERE message_id = $1", bot.sent[0]["message_id"])
    assert (stored["user_id"], stored["text"]) == (BOT_ID, "Ответ модели")


async def test_answer_with_link_is_archived_without_apology(make_env, conn):
    answer = "Это было в #1 (https://t.me/c/1000000001/1)."
    bot, llm, feed = make_env(FakeLLM(answer))
    await feed(update("message", message(1, "нестор, где это было?")))
    # The reference becomes a quote of the message, linked to it.
    sent = 'Это было в <a href="https://t.me/c/1000000001/1">«нестор, где это было?»</a>.'
    assert [(s["text"], s["parse_mode"]) for s in bot.sent] == [(sent, "HTML")]
    assert await conn.fetchval("SELECT text FROM messages WHERE message_id = $1", bot.sent[0]["message_id"]) == sent
    assert await conn.fetchval("SELECT answer FROM bot_requests") == answer   # the model's own words, for follow-ups
    assert await conn.fetchval("SELECT status FROM bot_requests") == "ok"


async def test_failed_archiving_of_own_answer_is_not_an_error(make_env, conn):
    bot, llm, feed = make_env()

    async def broken(msg):
        raise ValueError("unexpected Bot API shape")
    feed.answerer.save_outgoing = broken
    await feed(update("message", message(1, "нестор, привет")))
    assert [s["text"] for s in bot.sent] == ["Ответ модели"]  # no apology after a delivered answer
    assert await conn.fetchval("SELECT status FROM bot_requests") == "ok"


async def test_web_question_goes_to_web_profile_without_archive(make_env, conn):
    web = FakeLLM("В Таллине +12, источник: https://weather.example")
    bot, llm, feed = make_env(FakeLLM(prompts.WEB_MARKER), web=web)
    await feed(update("message", message(1, "секретный план Пети", uid=222)))
    await feed(update("message", message(2, "нестор, сколько лет Эйфелевой башне?", uid=111)))

    assert [s["text"] for s in bot.sent] == ["В Таллине +12, источник: https://weather.example"]
    assert "секретный план" in llm.calls[0][-1]["content"]  # the archive model saw the chat ...
    sent_to_web = "\n".join(m["content"] for m in web.calls[0])
    assert "сколько лет Эйфелевой башне?" in sent_to_web
    assert "секретный план" not in sent_to_web and "User" not in sent_to_web and "ticket" not in sent_to_web
    req = await conn.fetchrow("SELECT * FROM bot_requests")
    assert (req["scenario"], req["status"], req["input_tokens"], req["output_tokens"]) == ("web", "ok", 2000, 400)


async def test_plain_web_question_skips_the_archive_model(make_env, conn):
    web = FakeLLM("В Кирове +5")
    bot, llm, feed = make_env(FakeLLM("У меня нет доступа к интернету"), web=web)
    await feed(update("message", message(1, "Нестор, какая погода в Кирове?")))
    assert [s["text"] for s in bot.sent] == ["В Кирове +5"] and llm.calls == []
    req = await conn.fetchrow("SELECT * FROM bot_requests")
    assert (req["scenario"], req["input_tokens"], req["output_tokens"]) == ("web", 1000, 200)


async def test_web_follow_up_carries_only_web_turns(make_env):
    web = FakeLLM("Какой город?")
    bot, llm, feed = make_env(FakeLLM(prompts.WEB_MARKER), web=web)
    await feed(update("message", message(1, "нестор, какая погода?")))
    answer = {"message_id": bot.sent[0]["message_id"], "date": 1_790_000_000,
              "chat": {"id": CHAT_ID, "type": "supergroup"}, "from": {"id": BOT_ID, "is_bot": True, "first_name": "Н"},
              "text": "Какой город?"}
    await feed(update("message", message(2, "Таллин", reply_to_message=answer)))
    assert llm.calls == []  # a reply to a web answer goes straight back to the web profile
    assert [(m["role"], m["content"]) for m in web.calls[1][1:3]] == [("user", "какая погода?"),
                                                                      ("assistant", "Какой город?")]
    assert "Таллин" in web.calls[1][-1]["content"]


async def test_web_question_without_web_profile_is_refused(make_env, conn):
    bot, llm, feed = make_env(FakeLLM(prompts.WEB_MARKER))
    await feed(update("message", message(1, "нестор, курс евро?")))
    assert [s["text"] for s in bot.sent] == [prompts.NO_WEB]
    assert await conn.fetchval("SELECT scenario FROM bot_requests") == "web"


async def test_reply_to_answer_continues_conversation(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, кто прав?")))
    answer = {"message_id": bot.sent[0]["message_id"], "date": 1_790_000_000,
              "chat": {"id": CHAT_ID, "type": "supergroup"}, "from": {"id": BOT_ID, "is_bot": True, "first_name": "Н"},
              "text": "Ответ модели"}
    await feed(update("message", message(2, "а подробнее?", reply_to_message=answer)))
    second = llm.calls[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[1]["content"] == "кто прав?" and second[2]["content"] == "Ответ модели"
    parent, child = await conn.fetch("SELECT id, parent_id FROM bot_requests ORDER BY id")
    assert child["parent_id"] == parent["id"]


async def test_mentioned_user_messages_are_in_context(make_env, conn):
    bot, llm, feed = make_env()
    petya = message(1, "Пенсии надо отменить", uid=222)
    petya["from"]["username"] = "petya"
    await feed(update("message", petya))
    text = "нестор, оцени тезисы @petya"
    await feed(update("message", message(2, text, entities=[{"type": "mention", "offset": 21, "length": 6}])))
    assert "<сообщения User222 (@petya) за сутки>" in llm.calls[0][-1]["content"]


async def test_reply_target_thread_is_in_context(make_env):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "Тезис: Земля плоская", uid=222)))
    await feed(update("message", message(2, "нестор", reply_to_message=message(1, "Тезис: Земля плоская", uid=222))))
    user = llm.calls[0][-1]["content"]
    assert "ответ на сообщение #1" in user and "Оцени тезисы в сообщении" in user


async def test_rate_limit_per_user(make_env, conn):
    bot, llm, feed = make_env(rate_limit=2)
    for i in range(1, 4):
        await feed(update("message", message(i, "бот, привет")))
    assert len(llm.calls) == 2
    assert bot.sent[-1]["text"].startswith("⏳ Слишком часто")
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE status = 'rate_limited'") == 1


async def test_daily_token_limit(make_env):
    bot, llm, feed = make_env(daily_token_limit=1200)
    await feed(update("message", message(1, "бот, раз")))
    await feed(update("message", message(2, "бот, два", uid=333)))
    assert len(llm.calls) == 1 and bot.sent[-1]["text"] == prompts.RATE_LIMITED_DAY


async def test_progress_note_and_timeout(make_env, conn):
    bot, llm, feed = make_env(FakeLLM(delay=1), progress_after_s=0.05, timeout_s=0.2)
    await feed(update("message", message(1, "бот, думай")))
    texts = [s["text"] for s in bot.sent]
    assert texts == [prompts.STILL_THINKING, prompts.TIMEOUT]
    assert bot.deleted == [bot.sent[0]["message_id"]]
    assert await conn.fetchval("SELECT status FROM bot_requests") == "timeout"


async def test_llm_error_is_a_short_apology(make_env, conn):
    bot, llm, feed = make_env(FakeLLM(fail=True))
    await feed(update("message", message(1, "бот, привет")))
    assert bot.sent[-1]["text"] == prompts.ERROR
    assert await conn.fetchval("SELECT status FROM bot_requests") == "error"


async def test_one_job_per_chat_at_a_time(make_env, pool):
    bot, llm, feed = make_env(FakeLLM(delay=0.05))
    collector_feed = feed
    await asyncio.gather(*(collector_feed(update("message", message(i, "бот, вопрос", uid=100 + i))) for i in range(1, 4)))
    assert len(llm.calls) == 3 and llm.max_active == 1


async def test_edits_and_plain_messages_do_not_trigger(make_env):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "обычный разговор")))
    await feed(update("edited_message", message(1, "бот, а теперь?", edit_date=1_790_000_060)))
    assert llm.calls == [] and bot.sent == []


async def test_forget_me(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "моё личное", uid=222)))
    reply = message(2, "ответ", uid=111, reply_to_message=message(1, "моё личное", uid=222),
                    quote={"text": "моё личное", "position": 0})
    await feed(update("message", reply))
    await feed(update("message", message(3, "/forget_me", uid=222)))
    await feed(update("message", message(4, "после", uid=222)))

    assert await conn.fetchval("SELECT count(*) FROM messages WHERE user_id = 222") == 0
    assert "моё личное" not in str(await conn.fetchval("SELECT raw FROM messages WHERE message_id = 2"))
    assert bot.sent[-1]["text"].startswith("🧹 Готово: удалил из архива твои сообщения (2)")


async def test_pause_requires_admin_and_stops_archiving(make_env, conn):
    bot, llm, feed = make_env(admins={111})
    await feed(update("message", message(1, "/pause", uid=222)))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(update("message", message(2, "/pause", uid=111)))
    await feed(update("message", message(3, "секрет при паузе", uid=222)))
    await feed(update("message", message(4, "бот, ты тут?", uid=222)))
    await feed(update("message", message(5, "/resume", uid=111)))
    await feed(update("message", message(6, "снова пишем", uid=222)))
    texts = {r["message_id"] for r in await conn.fetch("SELECT message_id FROM messages WHERE user_id <> $1", BOT_ID)}
    assert 3 not in texts and 4 not in texts and 6 in texts
    assert llm.calls == [] and bot.sent[-1]["text"] == prompts.RESUMED


async def test_bot_word_switch(make_env):
    bot, llm, feed = make_env(admins={111})
    await feed(update("message", message(1, "/bot_word off", uid=111)))
    await feed(update("message", message(2, "бот, привет", uid=222)))
    await feed(update("message", message(3, "нестор, привет", uid=222)))
    assert len(llm.calls) == 1


async def test_summary_command_and_stats(make_env):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "обсуждаем отпуск", uid=222, date=int(datetime.now(timezone.utc).timestamp()))))
    await feed(update("message", message(2, "/summary неделя", uid=111)))
    assert "Сценарий: summary" in llm.calls[0][-1]["content"]
    assert "<сообщения за период>" in llm.calls[0][-1]["content"]
    await feed(update("message", message(3, "/stats", uid=111, date=int(datetime.now(timezone.utc).timestamp()))))
    assert bot.sent[-1]["text"].startswith(f"<b>{prompts.STATS_ACTIVITY}</b>")


async def test_mcp_tools_are_scoped_by_ticket(make_env, conn, reader):
    bot, llm, feed = make_env()
    other = -1001000000999
    await feed(update("message", message(1, "наш отпуск в Греции")))
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", other)
    await conn.execute(
        "INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw) VALUES ($1, 1, 111, now(), 'чужой отпуск', '{}')",
        other)
    await conn.execute("INSERT INTO request_tickets VALUES ('good', $1, NULL, now() + interval '1 min'), "
                       "('old', $1, NULL, now() - interval '1 min')", CHAT_ID)

    found = await tools.search_messages(reader, "good", "отпуск")
    assert "Греции" in found and "чужой" not in found
    assert "Греции" in await tools.get_recent(reader, "good")
    with pytest.raises(tools.ToolError):
        await tools.search_messages(reader, "old", "отпуск")
    with pytest.raises(tools.ToolError):
        await tools.get_recent(reader, "guess")


async def test_mcp_server_exposes_only_read_tools():
    from nestor.mcp.__main__ import server

    names = sorted(t.name for t in await server.list_tools())
    assert names == ["find_files", "find_links", "get_recent", "get_thread", "get_user_messages", "search_messages"]


# ---- formatting ----

async def test_model_markdown_is_sent_as_telegram_formatting(make_env, conn):
    answer = ("**Итог:** договорились ехать 🏖\n\n- Петя: [«беру палатку»](#1)\n- Вася: [«гитару»](#2)\n"
              "- кто-то: [«удалено»](#99)")
    bot, llm, feed = make_env(FakeLLM(answer))
    await feed(update("message", message(1, "беру палатку и котелок", uid=222)))
    await feed(update("message", message(2, "а я гитару", uid=333)))
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 333")   # Вася used /forget_me
    await feed(update("message", message(3, "нестор, кто что берёт?", uid=111)))
    sent = bot.sent[-1]
    assert sent["parse_mode"] == "HTML" and sent["text"] == (
        "<b>Итог:</b> договорились ехать 🏖\n\n"
        '• Петя: <a href="https://t.me/c/1000000001/1">«беру палатку»</a>\n'
        "• Вася: «гитару»\n"                 # an author who opted out: no quote of theirs, no link
        "• кто-то: «удалено»")
    assert "Стиль собеседника: пишет коротко." in llm.calls[0][-1]["content"]
    req = await conn.fetchrow("SELECT answer, quoted_user_ids FROM bot_requests")
    assert (req["answer"], req["quoted_user_ids"]) == (answer, [222])


async def test_summaries_keep_their_own_format(make_env):
    bot, llm, feed = make_env()
    now = int(datetime.now(timezone.utc).timestamp())
    await feed(update("message", message(1, "беру палатку", uid=222, date=now - 300)))
    await feed(update("message", message(2, "/summary", uid=111, date=now - 200)))
    await feed(update("message", message(3, "а я котелок", uid=222, date=now - 100)))
    await feed(update("message", message(4, "нестор, что я пропустил?", uid=111, date=now - 50)))
    await feed(update("message", message(5, "нестор, оцени тезисы", uid=111, date=now)))
    assert len(llm.calls) == 3 and not any("Стиль собеседника" in c[-1]["content"] for c in llm.calls)


async def test_forget_me_also_removes_answers_that_quote_them(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("Петя берёт: [«палатку»](#1)"))
    await feed(update("message", message(1, "беру палатку и секретный котелок", uid=222)))
    await feed(update("message", message(2, "нестор, кто что берёт?", uid=111)))
    answer_id = bot.sent[-1]["message_id"]
    assert "секретный" not in bot.sent[-1]["text"] and "палатку" in bot.sent[-1]["text"]
    reply = message(3, "точно", uid=111, reply_to_message={"message_id": answer_id, "date": 1, "chat": {"id": CHAT_ID,
                    "type": "supergroup"}, "from": {"id": BOT_ID, "is_bot": True, "first_name": "Нестор"}},
                    quote={"text": "палатку", "position": 0})
    await feed(update("message", reply))
    await feed(update("message", message(4, "/forget_me", uid=222)))

    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", answer_id) == 0
    assert "палатку" not in str(await conn.fetchval("SELECT raw FROM messages WHERE message_id = 3"))
    assert await conn.fetchval("SELECT answer FROM bot_requests WHERE scenario = 'other'") is None
    # The next request does not carry their words to the model in any form.
    await feed(update("message", message(5, "нестор, а теперь?", uid=111)))
    assert "палатк" not in str(llm.calls[-1])


async def test_ids_shared_by_a_migrated_chat_and_its_old_group_are_not_linked(make_env, conn):
    old = -4242
    bot, llm, feed = make_env(FakeLLM("Это сказал Петя: [«едем в Грецию»](#7), а Вася: [«ок»](#8)"))
    for i in range(1, 9):
        await feed(update("message", message(i, f"новая группа {i}", uid=111)))
    await feed.answerer.save_outgoing(message(7, "едем в Грецию", chat_id=old, uid=222)
                                      | {"chat": {"id": old, "type": "group", "title": "Old"}})
    await conn.execute("UPDATE chats SET migrated_from_chat_id = $1 WHERE chat_id = $2", old, CHAT_ID)
    await feed(update("message", message(9, "нестор, кто что сказал?", uid=111)))
    assert bot.sent[-1]["text"] == 'Это сказал Петя: «едем в Грецию», а Вася: ' \
                                   '<a href="https://t.me/c/1000000001/8">«ок»</a>'
    # Both people who might have written #7 are recorded: the quote itself is in the answer.
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests") == [111, 222]
    # A t.me/c link names the supergroup itself, so it is never ambiguous.
    llm.answer = "Вот: [«новая группа 7»](https://t.me/c/1000000001/7)"
    await feed(update("message", message(10, "нестор, а ссылкой?", uid=111)))
    assert bot.sent[-1]["text"] == 'Вот: <a href="https://t.me/c/1000000001/7">«новая группа 7»</a>'


async def test_a_number_the_model_was_not_shown_stays_a_number(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("Про issue #1234 спорили, см. [«купил хлеб»](#1234)"))
    three_days_ago = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp())
    await feed(update("message", message(1234, "купил хлеб и молоко", uid=333, date=three_days_ago)))
    await feed(update("message", message(2000, "/summary", uid=111, date=int(datetime.now(timezone.utc).timestamp()))))
    assert "#1234" not in llm.calls[0][-1]["content"]
    assert bot.sent[-1]["text"] == \
        'Про issue #1234 спорили, см. <a href="https://t.me/c/1000000001/1234">«купил хлеб»</a>'
    # Shown to the model, the same bare number is a reference.
    await feed(update("message", message(2001, "нестор, что там с хлебом?", uid=111)))
    assert "#1234" in llm.calls[1][-1]["content"]
    assert bot.sent[-1]["text"].startswith(
        'Про issue <a href="https://t.me/c/1000000001/1234">«купил хлеб и молоко»</a>')


async def test_forget_me_during_an_answer_in_flight_still_removes_it(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("Петя берёт: [«палатку и секретный котелок»](#1)", delay=0.4))
    await feed(update("message", message(1, "беру палатку и секретный котелок", uid=222)))
    asking = asyncio.create_task(feed(update("message", message(2, "нестор, кто что берёт?", uid=111))))
    await asyncio.sleep(0.15)                       # the model is thinking
    await feed(update("message", message(3, "/forget_me", uid=222)))
    await asking
    answer = next(m for m in bot.sent if m["text"].startswith("Петя берёт"))
    assert "секретный котелок" in answer["text"]   # the model's own label: nothing else was left to quote
    req = await conn.fetchrow("SELECT answer, quoted_user_ids FROM bot_requests WHERE scenario = 'other'")
    assert (req["answer"], req["quoted_user_ids"]) == (None, [222])
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", answer["message_id"]) == 0
    await feed(update("message", message(4, "нестор, а теперь?", uid=111)))
    assert "котелок" not in str(llm.calls[-1])


async def test_answers_that_repeat_an_earlier_answer_go_with_it(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("Петя берёт: [«палатку»](#1)"))
    await feed(update("message", message(1, "беру палатку", uid=222)))
    await feed(update("message", message(2, "нестор, кто что берёт?", uid=111)))
    first = bot.sent[-1]
    # An answer that quotes the bot's own answer: the people quoted there are quoted again.
    llm.answer = f"Я уже отвечал: [«Петя берёт: палатку»](#{first['message_id']})"
    await feed(update("message", message(3, "нестор, повтори, кто что берёт?", uid=333)))
    second = bot.sent[-1]
    assert second["text"] == \
        f'Я уже отвечал: <a href="https://t.me/c/1000000001/{first["message_id"]}">«Петя берёт: палатку»</a>'
    # A follow-up in the same conversation may repeat the quotes without a reference.
    llm.answer = "Да, Петя так и сказал про палатку."
    bot_msg = {"message_id": first["message_id"], "date": 1, "chat": {"id": CHAT_ID, "type": "supergroup"},
               "from": {"id": BOT_ID, "is_bot": True, "first_name": "Нестор"}, "text": first["text"]}
    await feed(update("message", message(4, "точно?", uid=111, reply_to_message=bot_msg)))
    third = bot.sent[-1]
    rows = await conn.fetch("SELECT quoted_user_ids FROM bot_requests ORDER BY id")
    assert [r["quoted_user_ids"] for r in rows] == [[222], [222], [222]]
    await feed(update("message", message(5, "/forget_me", uid=222)))
    gone = [first["message_id"], second["message_id"], third["message_id"]]
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = ANY($1::bigint[])", gone) == 0
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE answer IS NOT NULL") == 0


async def test_an_answer_about_a_replied_message_goes_with_its_author(make_env, conn):
    bot, llm, feed = make_env()
    claim = message(1, "налоги надо снизить срочно", uid=222)
    await feed(update("message", claim))
    await feed(update("message", message(2, "нестор, оцени тезисы", uid=111, reply_to_message=claim)))
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE scenario = 'theses'") == [222]


async def test_refused_formatting_is_sent_as_plain_text(make_env, conn):
    from aiogram.methods import SendMessage
    from aiogram.exceptions import TelegramBadRequest

    bot, llm, feed = make_env(FakeLLM("**Итог:** [сайт](https://a.example)"))
    bot.send_errors = [TelegramBadRequest(SendMessage(chat_id=CHAT_ID, text="x"),
                                          "Bad Request: can't parse entities")]
    await feed(update("message", message(1, "нестор, итог?")))
    assert [(s["text"], s["parse_mode"]) for s in bot.sent] == [("Итог: сайт (a.example)", None)]
    assert await conn.fetchval("SELECT status FROM bot_requests") == "ok"


async def test_long_writers_get_room_for_longer_answers(make_env):
    bot, llm, feed = make_env()
    for i in range(1, 4):
        await feed(update("message", message(i, "очень подробное рассуждение " * 12, uid=111)))
    await feed(update("message", message(4, "нестор, а ты что думаешь?", uid=111)))
    assert "Стиль собеседника: пишет развёрнуто." in llm.calls[0][-1]["content"]


async def test_a_bare_number_from_an_archive_tool_is_not_linked_but_goes_with_its_author(make_env, conn):
    # The archive tools show ids the prompt does not; the model may copy one bare. Not linked (it
    # may be a number), but the words beside it are the author's: /forget_me removes the answer.
    bot, llm, feed = make_env(FakeLLM("Вася писал об этом: #1234 — «купил хлеб и молоко»"))
    three_days_ago = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp())
    await feed(update("message", message(1234, "купил хлеб и молоко", uid=333, date=three_days_ago)))
    await feed(update("message", message(2000, "/summary", uid=111, date=int(datetime.now(timezone.utc).timestamp()))))
    assert "#1234" not in llm.calls[0][-1]["content"]
    answer = bot.sent[-1]
    assert answer["text"] == "Вася писал об этом: #1234 — «купил хлеб и молоко»"
    assert await conn.fetchval("SELECT quoted_user_ids FROM bot_requests WHERE scenario = 'summary'") == [333]
    await feed(update("message", message(3000, "/forget_me", uid=333)))
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = $1", answer["message_id"]) == 0
    assert await conn.fetchval("SELECT answer FROM bot_requests WHERE scenario = 'summary'") is None


async def test_thanks_for_an_answer_with_a_link_is_a_reaction_even_when_the_link_has_a_query(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("Вот: https://example.com/search?q=налоги"))
    await feed(update("message", message(1, "нестор, какую ссылку кидал Петя про налоги", uid=222)))
    answer = bot.sent[-1]
    telegram_view = {"message_id": answer["message_id"], "date": 1, "chat": {"id": CHAT_ID, "type": "supergroup"},
                     "from": {"id": BOT_ID, "is_bot": True, "first_name": "Нестор"}, "text": answer["text"]}
    await feed(update("message", message(2, "спасибо", uid=222, reply_to_message=telegram_view)))
    assert len(llm.calls) == 1 and (2, ["❤"]) in bot.reactions
    # A question in the answer itself still makes the «угу» an answer for the model.
    telegram_view["text"] = "Ты про https://example.com/search?q=налоги?"
    await feed(update("message", message(3, "угу", uid=222, reply_to_message=telegram_view)))
    assert len(llm.calls) == 2


# ---- retention: an answer leaves the archive with the oldest message it may carry ----

async def test_an_answer_records_the_oldest_message_it_was_shown(make_env, pool, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "старая мысль Пети", uid=222)))
    await conn.execute("UPDATE messages SET stored_at = now() - interval '300 days' WHERE message_id = 1")
    await feed(update("message", message(2, "нестор, что думаешь?", uid=111)))
    assert "старая мысль Пети" in llm.calls[0][-1]["content"]
    shown = await conn.fetchval("SELECT stored_at FROM messages WHERE message_id = 1")
    assert await conn.fetchval("SELECT oldest_source_at FROM bot_requests") == shown

    # The night message 1 leaves, the answer goes with it, though the answer itself is a day old.
    answer_id = bot.sent[-1]["message_id"]
    got = await retention.purge(pool, shown + timedelta(seconds=1), pause_s=0)
    assert {k: got[k] for k in ("answers", "answer_messages", "messages")} == \
        {"answers": 1, "answer_messages": 1, "messages": 1}
    req = await conn.fetchrow("SELECT question, answer FROM bot_requests")
    assert (req["question"], req["answer"]) == ("что думаешь?", None)
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE message_id = ANY($1::bigint[])",
                               [1, answer_id]) == 0


def _telegram_view(sent: dict) -> dict:
    """The bot's answer as Telegram shows it in a reply."""
    return {"message_id": sent["message_id"], "date": 1_790_000_000, "chat": {"id": CHAT_ID, "type": "supergroup"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Нестор"}, "text": sent["text"]}


async def test_a_follow_up_carries_the_oldest_source_of_its_conversation(make_env, conn):
    bot, _, feed = make_env()
    await feed(update("message", message(1, "нестор, кто прав?")))
    await conn.execute("UPDATE bot_requests SET oldest_source_at = now() - interval '400 days'")
    earlier = await conn.fetchval("SELECT oldest_source_at FROM bot_requests")
    await feed(update("message", message(2, "а подробнее?", reply_to_message=_telegram_view(bot.sent[-1]))))
    child = await conn.fetchrow("SELECT parent_id, oldest_source_at FROM bot_requests ORDER BY id DESC LIMIT 1")
    assert child["parent_id"] is not None and child["oldest_source_at"] == earlier


async def test_a_reference_to_the_bots_own_answer_carries_what_that_answer_did(make_env, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, кто что берёт?")))
    first = bot.sent[-1]
    await conn.execute("UPDATE bot_requests SET oldest_source_at = now() - interval '400 days'")
    earlier = await conn.fetchval("SELECT oldest_source_at FROM bot_requests")
    llm.answer = f"Я уже отвечал: [«Ответ модели»](#{first['message_id']})"
    await feed(update("message", message(2, "нестор, повтори", uid=333)))
    assert await conn.fetchval("SELECT oldest_source_at FROM bot_requests ORDER BY id DESC LIMIT 1") == earlier


async def test_a_reply_to_a_scrubbed_answer_continues_with_answer_removed(make_env, pool, conn):
    bot, llm, feed = make_env()
    await feed(update("message", message(1, "нестор, кто прав?")))
    first = bot.sent[-1]
    await conn.execute("UPDATE bot_requests SET oldest_source_at = now() - interval '400 days'")
    _, cutoff = await retention.db_times(conn)
    assert (await retention.purge(pool, cutoff, pause_s=0))["answers"] == 1
    # Telegram still shows the answer; the archive no longer has its words.
    await feed(update("message", message(2, "а подробнее?", reply_to_message=_telegram_view(first))))
    assert [(m["role"], m["content"]) for m in llm.calls[1][1:3]] == [("user", "кто прав?"),
                                                                      ("assistant", "(ответ удалён)")]
    assert "Ответ модели" not in str(llm.calls[1][1:])


class _PurgingLLM(FakeLLM):
    """The retention purge deletes the parent request while the follow-up is being written."""

    def __init__(self, pool, *, commit_after=None):
        super().__init__()
        self.pool, self.commit_after, self.batch = pool, commit_after, None

    async def complete(self, messages):
        if self.calls:                                    # the follow-up, not the first question
            if self.commit_after is None:
                async with self.pool.acquire() as c:
                    _, cutoff = await retention.db_times(c)
                await retention.purge(self.pool, cutoff, pause_s=0)
            else:                                         # a batch in flight: deleted, not committed yet
                self.batch = await self.pool.acquire()
                self.tx = self.batch.transaction()
                await self.tx.start()
                await forget.erase_lock(self.batch)
                await self.batch.execute("DELETE FROM bot_requests")
                self.commit = asyncio.create_task(self._commit())
        return await super().complete(messages)

    async def _commit(self):
        await asyncio.sleep(self.commit_after)
        await self.tx.commit()
        await self.pool.release(self.batch)


@pytest.mark.parametrize("commit_after", [None, 0.3], ids=["purged", "purging"])
async def test_a_follow_up_is_logged_when_the_purge_deletes_its_parent_meanwhile(make_env, pool, conn, commit_after):
    llm = _PurgingLLM(pool, commit_after=commit_after)
    bot, _, feed = make_env(llm)
    await feed(update("message", message(1, "нестор, кто прав?")))
    await conn.execute("UPDATE bot_requests SET created_at = now() - interval '13 months'")
    await feed(update("message", message(2, "а подробнее?", reply_to_message=_telegram_view(bot.sent[-1]))))
    if commit_after is not None:
        await llm.commit
    # The follow-up keeps its row, without the parent that left (as ON DELETE SET NULL would leave it).
    rows = await conn.fetch("SELECT trigger_msg_id, parent_id, status FROM bot_requests")
    assert [tuple(r) for r in rows] == [(2, None, "ok")]
    assert bot.reactions[-1] == (2, [])


async def test_privacy_says_from_when_and_backup_weeks(make_env):
    bot, _, feed = make_env()
    await feed(update("message", message(1, "/privacy")))
    text = bot.sent[-1]["text"]
    assert "12 месяцев с того дня" in text and "до 5 недель" in text and "{" not in text
    assert "не дольше суток" not in text     # the model server's history is not pruned yet: no such promise


async def test_forget_me_strips_imported_quotes_forwards_and_names(make_env, conn):
    bot, _, feed = make_env()
    petya = message(1, "моё личное", uid=222)
    petya["from"] |= {"last_name": "Петров", "username": "petya"}
    await feed(update("message", petya))
    # Вася forwards Петя's message from elsewhere: the row is Вася's, the words are Петя's.
    author = {"id": 222, "is_bot": False, "first_name": "User222", "last_name": "Петров", "username": "petya"}
    forward = message(3, "секрет Пети", uid=111, entities=[{"type": "bold", "offset": 0, "length": 6}],
                      forward_origin={"type": "user", "date": 1_789_000_000, "sender_user": author},
                      forward_from=author, forward_date=1_789_000_000)    # the legacy fields Telegram still sends
    await feed(update("message", forward))
    # An imported forward names its author by an id.
    origin = {"type": "user", "date": 1_789_000_000, "sender_id": 222}
    await conn.execute(
        "INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, forward_origin, raw) "
        "VALUES ($1, 5, 111, now(), 'ещё секрет Пети', $2, $3)", CHAT_ID, origin,
        {"message_id": 5, "text": "ещё секрет Пети", "forward_origin": origin, "import_source": "telegram_import"})
    # An imported reply: Telegram's export does not say who wrote the replied message.
    await conn.execute(
        "INSERT INTO messages (chat_id, message_id, user_id, sent_at, reply_to_id, text, raw) "
        "VALUES ($1, 2, 111, now(), 1, 'согласен', $2)", CHAT_ID,
        {"message_id": 2, "text": "согласен", "reply_to_message": {"message_id": 1},
         "quote": {"text": "моё личное"}, "import_source": "telegram_import"})
    await feed(update("message", message(4, "/forget_me", uid=222)))
    assert bot.sent[-1]["text"].startswith("🧹 Готово: удалил из архива твои сообщения (2)")

    rows = {r["message_id"]: r for r in await conn.fetch("SELECT * FROM messages WHERE message_id IN (2, 3, 5)")}
    assert "quote" not in rows[2]["raw"] and "личное" not in str(rows[2]["raw"])
    for fwd in (rows[3], rows[5]):
        assert (fwd["user_id"], fwd["text"], fwd["entities"]) == (111, None, None)
        assert fwd["forward_origin"] == {"type": "user", "sender_user": {"id": 222}}
        assert fwd["raw"]["forward_origin"] == {"type": "user", "sender_user": {"id": 222}}
        assert not {"text", "entities", "forward_from"} & set(fwd["raw"])
        assert "Петров" not in str(fwd["raw"]) and "секрет" not in str(fwd["raw"])
    names = await conn.fetchrow("SELECT username, first_name, last_name, opted_out FROM users WHERE user_id = 222")
    assert tuple(names) == (None, None, None, True)
