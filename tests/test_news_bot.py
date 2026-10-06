"""The news digest and model routing through the real dispatcher: /news, the evening post,
«нестор, подумай»."""
import json
import re
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from nestor.bot import context, markup, prompts
from nestor.news.model import FetchResult

from .factories import CHAT_ID, message, update
from .test_answerer import BOT_ID, FakeLLM, make_env_fixture  # noqa: F401 - fixture make_env
from .test_news_pipeline import FakeAdapter, ScriptedLLM, item, keep_all, make_service

TZ = ZoneInfo("Europe/Moscow")     # the answerer's default zone: local times here are its times


def ago(**delta) -> int:
    return int((datetime.now(timezone.utc) - timedelta(**delta)).timestamp())


def local(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(datetime.now(TZ).date(), dtime(hour, minute), TZ)


def plan_all(messages):
    """The digest model: every candidate a topic of its own, the first one in «Главное»."""
    numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.M)]
    return json.dumps({"items": [{"ids": [n], "section": "main" if i == 0 else "policy",
                                  "text": f"Тема номер {n}, пересказ."} for i, n in enumerate(numbers)],
                       "comment": "Итог дня."}, ensure_ascii=False)


async def with_news(make_env, pool, *, smart, **cfg):
    bot, llm, feed = make_env(smart=smart, admins={111}, **cfg)
    await feed(update("message", message(1, "привет", date=ago(minutes=5))))
    adapter = FakeAdapter()
    service = make_service(pool, adapter, ScriptedLLM(keep_all))
    service.is_wanted = feed.answerer.news_wanted
    feed.answerer.news = service
    await read_sources(service, adapter)
    return bot, llm, feed


async def read_sources(service, adapter):
    """What the news loop does on its own: the first run seeds, the next one brings three stories."""
    now = datetime.now(timezone.utc)
    await service.collect(now - timedelta(hours=7))     # first run: seeds
    adapter.results = {"mm": FetchResult([item("a", "Senate approves cannabis rescheduling", 1),
                                          item("b", "Governor signs psychedelic therapy law", 1)]),
                       "pubmed": FetchResult([item("1", "Psilocybin for depression: a randomized trial", 1)])}
    for i in adapter.results["mm"].items + adapter.results["pubmed"].items:
        i.published_at = now - timedelta(hours=1)
    await service.collect(now)


async def test_news_digest_is_posted_once_to_groups_that_have_it(make_env, pool, conn):
    smart = ScriptedLLM(plan_all, profile="smart")
    bot, llm, feed = await with_news(make_env, pool, smart=smart, news_chat_ids=frozenset({CHAT_ID}),
                                     news_time=dtime(20, 0))
    answerer = feed.answerer
    assert await answerer.news_wanted()
    await answerer.run_daily_posts(local(19, 59))
    await answerer.drain()
    assert not [s for s in bot.sent if s.get("parse_mode") == "HTML"]

    await answerer.run_daily_posts(local(20, 1))
    await answerer.drain()
    post = bot.sent[-1]
    assert post["parse_mode"] == "HTML" and post["reply_to"] is None
    assert post["text"].startswith("🗞 <b>Новости дня, ") and "<b>Главное за день</b>" in post["text"]
    assert '<a href="https://example.org/a">Marijuana Moment</a>' in post["text"]
    assert post["link_preview_options"].is_disabled
    row = await conn.fetchrow("SELECT status, message_id FROM daily_posts WHERE kind = 'news'")
    assert tuple(row) == ("posted", post["message_id"])
    await answerer.run_daily_posts(local(20, 30))
    await answerer.drain()
    assert bot.sent[-1] is post and len(smart.calls) == 1
    assert llm.calls == []          # the archive model is never involved


async def test_news_waits_for_the_sources_when_due_before_they_are_read(make_env, pool, conn):
    # The first start after NEWS_TIME, or «/news on» in the evening: the post is due before the news loop has run.
    smart = ScriptedLLM(plan_all, profile="smart")
    bot, llm, feed = make_env(smart=smart, news_chat_ids=frozenset({CHAT_ID}), news_time=dtime(20, 0))
    await feed(update("message", message(1, "привет", date=ago(minutes=5))))
    adapter = FakeAdapter()
    service = make_service(pool, adapter, ScriptedLLM(keep_all))
    answerer = feed.answerer
    answerer.news = service
    await answerer.run_daily_posts(local(20, 1))
    await answerer.drain()
    assert await conn.fetchval("SELECT status FROM daily_posts WHERE kind = 'news'") == "waiting"
    assert smart.calls == [] and not bot.sent

    await read_sources(service, adapter)
    await answerer.run_daily_posts(local(20, 5))        # too soon for another try
    await answerer.drain()
    assert not bot.sent
    await conn.execute("UPDATE daily_posts SET updated_at = now() - interval '11 minutes'")
    await answerer.run_daily_posts(local(20, 12))
    await answerer.drain()
    assert bot.sent[-1]["text"].startswith("🗞 <b>Новости дня, ")
    assert tuple(await conn.fetchrow("SELECT status, attempts FROM daily_posts WHERE kind = 'news'")) == ("posted", 2)


async def test_waiting_post_gives_up_after_six_tries(pool, conn):
    from nestor.bot import daily

    day = datetime.now(TZ).date()
    post = await daily.claim(conn, CHAT_ID, "news", day)
    for attempt in range(2, 7):
        await daily.finish(conn, post, "waiting")
        assert await daily.claim(conn, CHAT_ID, "news", day) is None        # within 10 minutes
        await conn.execute("UPDATE daily_posts SET updated_at = now() - interval '11 minutes'")
        assert await daily.claim(conn, CHAT_ID, "news", day) == post, attempt
    await daily.finish(conn, post, "waiting")
    await conn.execute("UPDATE daily_posts SET updated_at = now() - interval '11 minutes'")
    assert await daily.claim(conn, CHAT_ID, "news", day) is None
    assert await conn.fetchval("SELECT attempts FROM daily_posts WHERE id = $1", post) == 6


async def test_news_command_is_for_admins_and_switches_the_digest(make_env, pool, conn):
    smart = ScriptedLLM(plan_all, profile="smart")
    bot, llm, feed = await with_news(make_env, pool, smart=smart)
    answerer = feed.answerer
    assert not await answerer.news_wanted()        # not in NEWS_CHAT_IDS: off until an admin switches it on
    await feed(update("message", message(2, "/news on", uid=222, date=ago())))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(update("message", message(3, "/news on", date=ago())))
    assert markup.to_plain(bot.sent[-1]["text"]).startswith("🗞 Новостная сводка включена. Она приходит в 20:00")
    assert await answerer.news_wanted()
    await feed(update("message", message(4, "/news status", date=ago())))
    status = bot.sent[-1]["text"]
    assert status.startswith(prompts.NEWS_STATUS_HEADER)
    assert re.search(r"^mm: ok, \d\d\.\d\d \d\d:\d\d, 2 / 0 / 0$", status, re.M)
    await feed(update("message", message(5, "/news off", date=ago())))
    assert markup.to_plain(bot.sent[-1]["text"]).startswith("🗞 Новостная сводка выключена.")
    assert not await answerer.news_wanted()
    await answerer.run_daily_posts(local(23, 0))
    await answerer.drain()
    assert await conn.fetchval("SELECT count(*) FROM daily_posts WHERE kind = 'news'") == 0


async def test_think_goes_to_the_bigger_models(make_env, conn):
    deep = ScriptedLLM("Позиции сторон по существу…", profile="deep")
    smart = ScriptedLLM("В Таллине +8, источник: …", profile="smart")
    web = ScriptedLLM("В Таллине +8", profile="web")
    bot, llm, feed = make_env(deep=deep, smart=smart, web=web)
    await feed(update("message", message(1, "Петя: налоги надо снизить", uid=222, date=ago(minutes=3))))
    await feed(update("message", message(2, "нестор, кто прав?", date=ago(minutes=2))))
    assert len(deep.calls) == 1 and llm.calls == []      # disputes go to `deep` by default
    await feed(update("message", message(3, "нестор, о чём говорили сегодня?", date=ago(minutes=2))))
    assert len(llm.calls) == 1 and len(deep.calls) == 1
    await feed(update("message", message(4, "нестор, подумай, о чём говорили сегодня?", date=ago(minutes=1))))
    assert len(deep.calls) == 2 and "о чём говорили сегодня?" in deep.calls[-1][-1]["content"]
    await feed(update("message", message(5, "нестор, подумай: какая погода в Таллине?", date=ago())))
    assert len(smart.calls) == 1 and web.calls == []
    assert "Вопрос: какая погода в Таллине?\nСтиль собеседника:" in smart.calls[0][-1]["content"]
    assert context.SHORT_STYLE not in smart.calls[0][-1]["content"]      # «подумай»: the length follows the asker
    rows = [tuple(r) for r in await conn.fetch(
        "SELECT trigger_msg_id, scenario, profile FROM bot_requests ORDER BY id")]
    assert rows == [(2, "who_is_right", "deep"), (3, "summary", None), (4, "summary", "deep"), (5, "web", "smart")]


async def test_think_about_the_world_needs_only_the_smart_profile(make_env, conn):
    smart = ScriptedLLM("В Таллине +8, источник: …", profile="smart")
    bot, llm, feed = make_env(smart=smart)                # no cheap `web` profile
    await feed(update("message", message(1, "нестор, подумай: какая погода в Таллине?", date=ago())))
    assert len(smart.calls) == 1 and llm.calls == []
    assert bot.sent[-1]["text"] == "В Таллине +8, источник: …"
    assert tuple(await conn.fetchrow("SELECT scenario, profile FROM bot_requests")) == ("web", "smart")


async def test_routes_from_settings_keep_disputes_on_the_cheap_profile(make_env, conn):
    deep = ScriptedLLM("глубоко", profile="deep")
    bot, llm, feed = make_env(deep=deep, routes={"theses": "group", "who_is_right": "group"})
    await feed(update("message", message(1, "Петя: налоги надо снизить", uid=222, date=ago(minutes=3))))
    await feed(update("message", message(2, "нестор, кто прав?", date=ago(minutes=2))))
    assert deep.calls == [] and len(llm.calls) == 1


async def test_bare_think_reply_reconsiders_a_web_answer_on_the_bigger_model(make_env):
    web = ScriptedLLM("В Таллине +8", profile="web")
    smart = ScriptedLLM("Если подумать, к вечеру +5", profile="smart")
    bot, llm, feed = make_env(web=web, smart=smart)
    await feed(update("message", message(1, "нестор, какая погода в Таллине?", date=ago(minutes=2))))
    assert "Вопрос: какая погода в Таллине?\n" + context.SHORT_STYLE in web.calls[0][-1]["content"]
    answer = {"message_id": bot.sent[-1]["message_id"], "date": ago(minutes=1),
              "chat": {"id": CHAT_ID, "type": "supergroup"}, "from": {"id": BOT_ID, "is_bot": True, "first_name": "Н"},
              "text": "В Таллине +8"}
    await feed(update("message", message(2, "подумай хорошенько", reply_to_message=answer, date=ago())))
    assert bot.sent[-1]["text"] == "Если подумать, к вечеру +5" and len(web.calls) == 1
    assert "Вопрос: подумай хорошенько\nСтиль собеседника:" in smart.calls[0][-1]["content"]


async def test_archive_ticket_outlives_the_bigger_models_timeout(make_env, pool):
    class TicketLLM(ScriptedLLM):
        """Notes how long the archive ticket has left while the model is answering."""
        async def complete(self, messages):
            self.left = await pool.fetchval("SELECT extract(epoch FROM max(expires_at) - now()) FROM request_tickets")
            return await super().complete(messages)

    deep, group = TicketLLM("Позиции сторон…", profile="deep"), TicketLLM("Говорили о налогах.", profile="group")
    bot, llm, feed = make_env(group, deep=deep, timeout_s=120, smart_timeout_s=240)
    await feed(update("message", message(1, "Петя: налоги надо снизить", uid=222, date=ago(minutes=3))))
    await feed(update("message", message(2, "нестор, кто прав?", date=ago(minutes=2))))
    await feed(update("message", message(3, "нестор, о чём говорили?", date=ago(minutes=1))))
    assert deep.left > 240 and 120 < group.left <= 150      # deep: 240 s + 30; group: 120 s + 30
