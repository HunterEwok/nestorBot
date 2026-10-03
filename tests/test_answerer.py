"""Stage 3 through the real dispatcher with a fake Telegram and a fake LLM."""
import asyncio
import itertools
from datetime import datetime, timezone

import pytest
from aiogram import Dispatcher
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Message, Update

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

    async def send_message(self, chat_id, text, reply_parameters=None, **kw):
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
        self.reactions.append((message_id, [r.emoji for r in reaction or []]))
        return True

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

    def __init__(self, videos=None, spotify=(), apple=None):
        self.videos = videos or {}          # video id -> title; missing ids do not exist or cannot be embedded
        self.spotify_ids = set(spotify)
        self.apple_url = apple
        self.calls: list = []

    async def youtube(self, video_id):
        self.calls.append(("youtube", video_id))
        return {"title": self.videos[video_id]} if video_id in self.videos else None

    async def spotify(self, track_id):
        self.calls.append(("spotify", track_id))
        return {"title": "track"} if track_id in self.spotify_ids else None

    async def apple(self, artist, title):
        self.calls.append(("apple", artist, title))
        return self.apple_url


@pytest.fixture(name="make_env")
def make_env_fixture(pool):
    def make(llm=None, admins=(), web=None, smart=None, checker=None, no_private=(), **cfg):
        bot, llm = FakeBot(admins, no_private), llm or FakeLLM()
        settings = Settings(bot_token="42:TEST", database_url="", allowed_chat_ids=frozenset({CHAT_ID}))
        collector = Collector(settings, pool)
        cfg.setdefault("allowed_chat_ids", frozenset({CHAT_ID}))
        collector.answerer = Answerer(bot, pool, llm, BotIdentity(BOT_ID, "ewok_nestor_bot", ("нестор", "бот")),
                                      lambda m: collector.store(m, is_edit=False), AnswerConfig(**cfg), web_llm=web,
                                      smart_llm=smart, checker=checker or FakeChecker())
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
    answer = "Это было в #1 (https://t.me/c/1234567890/1)."
    bot, llm, feed = make_env(FakeLLM(answer))
    await feed(update("message", message(1, "нестор, где это было?")))
    assert [s["text"] for s in bot.sent] == [answer]
    assert await conn.fetchval("SELECT text FROM messages WHERE message_id = $1", bot.sent[0]["message_id"]) == answer
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
    assert bot.sent[-1]["text"].startswith("Слишком часто")
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
    assert bot.sent[-1]["text"].startswith("Готово: удалил из архива твои сообщения (2)")


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
    assert bot.sent[-1]["text"].startswith("Активность за 7 дней")


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
