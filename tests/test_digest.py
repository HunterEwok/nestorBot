"""The group digest once every N days (DIGEST_EVERY_DAYS, /digest N): when it is due, what period
it retells, notes on parts of a long period, the cap on what it reads, the daily token limit,
retries, its own queue, retention and /forget_me. The digest of one day as it always was:
test_features.py."""
import asyncio
import re
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from nestor.bot import answerer, context, daily, markup, prompts
from nestor.bot.llm import Completion

from .factories import CHAT_ID, message, update
from .test_answerer import BOT_ID, FakeLLM, make_env_fixture  # noqa: F401 - make_env

TZ = ZoneInfo("Europe/Moscow")     # the answerer's default zone
DAY = date(2026, 10, 5)            # the morning the digest comes; it retells the days before it
LINK = "https://t.me/c/1000000001/"


def at(day: date, hour: int, minute: int = 0, tz: ZoneInfo = TZ, **kw) -> datetime:
    return datetime.combine(day, dtime(hour, minute, **kw), tz)


def spread(first: datetime, until: datetime, n: int) -> list[datetime]:
    """n times evenly over [first, until)."""
    step = (until - first) / n
    return [first + i * step for i in range(n)]


async def write(conn, times: list[datetime], first: int = 1000, chat_id: int = CHAT_ID) -> list[int]:
    """Messages of the test group at these times, by users 200..209 in turn; their ids."""
    await conn.execute("INSERT INTO chats (chat_id, type, title) VALUES ($1, 'supergroup', 'Test group') "
                       "ON CONFLICT DO NOTHING", chat_id)
    await conn.execute("INSERT INTO users (user_id, first_name) SELECT g, 'User' || g FROM generate_series(200, 209) g "
                       "ON CONFLICT DO NOTHING")
    rows = [(chat_id, first + i, 200 + i % 10, t, f"обсуждаем тему {first + i}") for i, t in enumerate(times)]
    await conn.executemany("INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw) "
                           "VALUES ($1, $2, $3, $4, $5, '{}')", rows)
    return [r[1] for r in rows]


async def spend(conn, tokens: int) -> None:
    """Tokens spent today on something else."""
    await conn.execute("INSERT INTO bot_requests (chat_id, scenario, input_tokens, output_tokens, status) "
                       "VALUES ($1, 'other', $2, 0, 'ok')", CHAT_ID, tokens)


async def digest_on(conn, chat_id: int = CHAT_ID, **settings) -> None:
    await conn.execute("UPDATE chats SET settings = settings || $2::jsonb WHERE chat_id = $1",
                       chat_id, {"digest": True, **settings})


async def run(feed, now: datetime) -> None:
    await feed.answerer.run_daily_posts(now)
    await feed.answerer.drain()


def shown(call: list[dict]) -> list[int]:
    """Ids of the archive messages a prompt rendered for the model."""
    return [int(i) for i in re.findall(r"^#(\d+) ", call[-1]["content"], re.MULTILINE)]


class PartsLLM(FakeLLM):
    """Notes quote one message of their part: the first one by User20<quote> if it has one, else its
    first; the digest quotes the first note's message (or the first message it was shown). Other
    prompts get FakeLLM's answer. hook(call number) runs while a call is in flight; every call
    reports `tokens`, input and output."""

    def __init__(self, final: str | None = None, hook=None, quote: int = 0, tokens: tuple[int, int] = (1000, 200)):
        super().__init__()
        self.final, self.hook, self.quote, self.tokens = final, hook, quote, tokens

    async def complete(self, messages):
        done = await super().complete(messages)
        if self.hook is not None:
            await self.hook(len(self.calls))
        system, user = messages[0]["content"], messages[-1]["content"]
        if system == prompts.DIGEST_NOTES_SYSTEM:
            ids = re.findall(r"^#(\d+) ", user, re.MULTILINE)
            first = next((i for i in ids if int(i) % 10 == self.quote), ids[0])
            text = f"- тема части: #{first} «обсуждаем тему {first}» (User20{int(first) % 10})"
        elif system in (prompts.DIGEST_SYSTEM, prompts.DIGEST_PERIOD_SYSTEM):
            first = (re.search(r"#(\d+) «", user) or re.search(r"^#(\d+) ", user, re.MULTILINE)).group(1)
            text = self.final or f"- 📌 **начало**: [«обсуждаем тему {first}»](#{first})"
        else:
            return done
        return Completion(text, done.model, *self.tokens)


# ---- when it comes ----

async def test_the_digest_comes_once_every_n_days(make_env, conn):
    # DIGEST_EVERY_DAYS as it ships: 30. A message a day at noon, long before and long after.
    bot, llm, feed = make_env(FakeLLM("- 📌 **тема**: о чём говорили"))
    await write(conn, [at(DAY + timedelta(days=d), 12) for d in range(-40, 100)])
    await digest_on(conn)
    for d in range(93):
        bot.bot_status = ChatMemberStatus.LEFT if d == 30 else ChatMemberStatus.MEMBER      # day 30: skipped
        if d == 61:     # day 61: written, but Telegram does not take it
            bot.send_errors = [TelegramNetworkError(SendMessage(chat_id=CHAT_ID, text="x"), "timeout")]
        await run(feed, at(DAY + timedelta(days=d), 9, 30))
    rows = await conn.fetch("SELECT day, status FROM daily_posts WHERE kind = 'digest' ORDER BY day")
    # Right away when switched on; then 30 days after the last one posted: a skipped or failed day does
    # not count, the next morning tries again.
    assert [((r["day"] - DAY).days, r["status"]) for r in rows] == [
        (0, "posted"), (30, "skipped"), (31, "posted"), (61, "failed"), (62, "posted"), (92, "posted")]
    assert len(llm.calls) == 5
    # Each retells the 30 days before its morning, and nothing else.
    second = llm.calls[1]
    assert second[0]["content"] == prompts.DIGEST_PERIOD_SYSTEM
    assert second[1]["content"].startswith("Чат «Test group», дайджест за 30 дней, 6 октября — 4 ноября.\n\n")
    assert shown(second) == list(range(1040 + 1, 1040 + 31))        # the noons of DAY+1 .. DAY+30
    posts = [s["text"] for s in bot.sent if s["text"].startswith("☀️")]
    assert [p.split("\n")[0] for p in posts] == [
        "☀️ <b>Дайджест за 30 дней, 5 сентября — 4 октября</b>",
        "☀️ <b>Дайджест за 30 дней, 6 октября — 4 ноября</b>",
        "☀️ <b>Дайджест за 30 дней, 6 ноября — 5 декабря</b>",
        "☀️ <b>Дайджест за 30 дней, 6 декабря — 4 января</b>"]


async def test_with_one_day_it_is_the_morning_digest_as_before(make_env, conn):
    bot, llm, feed = make_env(FakeLLM("- 📌 **тема**: о чём говорили"))
    await write(conn, [at(DAY + timedelta(days=d), h) for d in range(-3, 5) for h in range(10, 16)])
    await digest_on(conn, digest_days=1)        # the group's own period beats DIGEST_EVERY_DAYS=30
    for d in range(4):
        await run(feed, at(DAY + timedelta(days=d), 8))         # before DIGEST_TIME: nothing
        await run(feed, at(DAY + timedelta(days=d), 9, 30))
        await run(feed, at(DAY + timedelta(days=d), 10))        # once a day
    assert await conn.fetchval("SELECT array_agg(status ORDER BY day) FROM daily_posts") == ["posted"] * 4
    assert len(llm.calls) == 4
    assert [c[0]["content"] for c in llm.calls] == [prompts.DIGEST_SYSTEM] * 4
    assert llm.calls[0][1]["content"].startswith("Чат «Test group», дайджест за 4 октября.\n\n<сообщения за вчера>")
    assert len(shown(llm.calls[0])) == 6
    assert [s["text"].split("\n")[0] for s in bot.sent] == [
        f"☀️ <b>Дайджест за вчера, {daily.day_name(DAY + timedelta(days=d - 1))}</b>" for d in range(4)]


async def test_the_period_follows_local_midnights_across_a_clock_change(make_env, conn):
    berlin = ZoneInfo("Europe/Berlin")      # clocks go back on 25 October 2026, 03:00 -> 02:00
    bot, llm, feed = make_env(FakeLLM("- 📌 **тема**"), tz=berlin, digest_every_days=2)
    day = date(2026, 10, 26)
    inside = [at(date(2026, 10, 24), 0, tz=berlin), at(date(2026, 10, 24), 18, tz=berlin),
              at(date(2026, 10, 25), 2, 30, tz=berlin), at(date(2026, 10, 25), 2, 30, tz=berlin, fold=1),
              at(date(2026, 10, 25), 23, 59, tz=berlin)]
    outside = [at(date(2026, 10, 23), 23, 59, tz=berlin), at(day, 0, tz=berlin)]
    await write(conn, outside + inside)
    await digest_on(conn)
    await run(feed, at(day, 9, 30, tz=berlin))
    assert shown(llm.calls[0]) == [1002, 1003, 1004, 1005, 1006]
    assert bot.sent[-1]["text"].startswith("☀️ <b>Дайджест за 2 дня, 24 октября — 25 октября</b>")


# ---- a long period: notes on its parts ----

async def test_a_long_period_is_read_in_parts_and_the_digest_quotes_any_of_them(make_env, conn):
    bot, llm, feed = make_env(PartsLLM())
    since, until = daily.digest_bounds(DAY, 30, TZ)
    ids = await write(conn, spread(since, until, 1200))
    await write(conn, [since - timedelta(minutes=1), until], first=5000)        # just outside the period
    aged = await conn.fetchval("UPDATE messages SET stored_at = now() - interval '100 days' "
                               "WHERE message_id = 1600 RETURNING stored_at")   # entered the archive long ago
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))

    # Three consecutive parts of 400, each in its own call, every message of the period in one of them.
    notes, final = llm.calls[:3], llm.calls[3]
    assert len(llm.calls) == 4 and [c[0]["content"] for c in notes] == [prompts.DIGEST_NOTES_SYSTEM] * 3
    assert [shown(c) for c in notes] == [ids[:400], ids[400:800], ids[800:]]
    assert "Часть 1 из 3, 5 сентября — 14 сентября." in notes[0][1]["content"]
    # The digest is written from the notes only, which keep the numbers of the key messages.
    assert final[0]["content"] == prompts.DIGEST_PERIOD_SYSTEM
    user = final[1]["content"]
    assert user.startswith("Чат «Test group», дайджест за 30 дней, 5 сентября — 4 октября.\n\n"
                           + prompts.DIGEST_NOTES_INTRO)
    assert all(f"#{i} «обсуждаем тему {i}»" in user for i in (1000, 1400, 1800)) and shown(final) == []
    # It quotes a message from the first days of the period: the link is built for it.
    post = bot.sent[-1]["text"]
    assert post.startswith("☀️ <b>Дайджест за 30 дней, 5 сентября — 4 октября</b>")
    assert f'<a href="{LINK}1000">«обсуждаем тему 1000»</a>' in post

    # Every call is its own row and counts toward the daily limit; the notes are not kept.
    rows = await conn.fetch("SELECT * FROM bot_requests ORDER BY id")
    assert [(r["scenario"], r["input_tokens"], r["output_tokens"]) for r in rows] == \
        [("digest_notes", 1000, 200)] * 3 + [("digest", 1000, 200)]
    assert [r["answer"] for r in rows[:3]] == [None] * 3
    async with feed.answerer.pool.acquire() as c:
        assert await feed.answerer._tokens_today(c) == 4 * 1200
    # The digest leaves with the oldest of all the messages it was made from, and with whoever it quotes.
    digest = rows[-1]
    assert (digest["oldest_source_at"], digest["quoted_user_ids"]) == (aged, [200])
    assert digest["answer_msg_ids"] == [bot.sent[-1]["message_id"]]
    assert await conn.fetchval("SELECT status FROM daily_posts") == "posted"


async def test_the_digest_links_and_names_messages_of_every_part(make_env, conn):
    async def forget_201(call: int) -> None:
        if call == 4:       # while the digest itself is written
            await feed.answerer.forget_user(201)

    final = "- 📌 **начало**: см. #1000\n- 💬 **потом**: [«обсуждаем тему 1401»](#1401)"
    bot, llm, feed = make_env(PartsLLM(final=final, hook=forget_201))
    since, until = daily.digest_bounds(DAY, 30, TZ)
    await write(conn, spread(since, until, 1200))
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    assert len(llm.calls) == 4
    # A bare number of a message from the first part is linked, as the model was shown it in the notes ...
    assert f'см. <a href="{LINK}1000">«обсуждаем тему 1000»</a>' in bot.sent[-1]["text"]
    # ... and a quote from the second part whose author used /forget_me meanwhile still names them: the
    # digest leaves the log as any answer that carries their words.
    row = await conn.fetchrow("SELECT answer, quoted_user_ids FROM bot_requests WHERE scenario = 'digest'")
    assert (row["answer"], row["quoted_user_ids"]) == (None, [200, 201])


async def test_a_busy_day_is_its_latest_messages_in_one_call(make_env, conn):
    bot, llm, feed = make_env(PartsLLM(), digest_every_days=1)
    ids = await write(conn, spread(at(DAY - timedelta(days=1), 0), at(DAY, 0), 600))
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    # What the morning digest always read: the latest 500 of yesterday, in one call.
    assert [shown(c) for c in llm.calls] == [ids[100:]] and llm.calls[0][0]["content"] == prompts.DIGEST_SYSTEM
    assert bot.sent[-1]["text"].startswith("☀️ <b>Дайджест за вчера, 4 октября</b>")
    assert await conn.fetchval("SELECT array_agg(scenario) FROM bot_requests") == ["digest"]


@pytest.mark.parametrize("n, read", [(500, [500]), (501, [250, 251, 0])])
async def test_one_call_reads_500_messages_at_most(make_env, conn, n, read):
    _, llm, feed = make_env(PartsLLM())
    since, until = daily.digest_bounds(DAY, 30, TZ)
    await write(conn, spread(since, until, n))
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    assert [len(shown(c)) for c in llm.calls] == read
    assert llm.calls[-1][0]["content"] == prompts.DIGEST_PERIOD_SYSTEM


async def test_beyond_the_cap_the_period_is_thinned_evenly(make_env, conn):
    bot, llm, feed = make_env(PartsLLM(final="- тема: " + "очень длинно " * 600))
    since, until = daily.digest_bounds(DAY, 30, TZ)
    ids = await write(conn, spread(since, until, 3000))         # 100 a day
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))

    parts = [shown(c) for c in llm.calls[:-1]]
    assert answerer.DIGEST_MAX_MESSAGES == 2000 and [len(p) for p in parts] == [500] * 4
    read = [i for p in parts for i in p]
    assert read == sorted(read) and read[0] == ids[0] and read[-1] == ids[-1]
    # Two of every three, all over the period: every day keeps its share, the last days no more than the first.
    assert read[:6] == [ids[0], ids[2], ids[3], ids[5], ids[6], ids[8]]
    per_day = [sum(1 for i in read if (i - 1000) // 100 == d) for d in range(30)]
    assert all(66 <= n <= 67 for n in per_day)
    # A digest that overran its length is still one message.
    post = bot.sent[-1]["text"]
    assert markup.visible_len(post) <= 4096 and post.endswith("…")
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE scenario = 'digest_notes'") == 4


async def test_bots_are_not_retold_and_take_no_share_of_the_cap(make_env, conn):
    _, llm, feed = make_env(PartsLLM())
    since, until = daily.digest_bounds(DAY, 30, TZ)
    humans = await write(conn, spread(since, until, answerer.DIGEST_MAX_MESSAGES))
    # The bot's own posts, from the last digest on the first morning of the period on, and another bot's.
    await conn.execute("INSERT INTO users (user_id, first_name, is_bot) VALUES ($1, 'Нестор', TRUE), "
                       "(299, 'Spam', TRUE)", BOT_ID)
    await conn.executemany("INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw) "
                           "VALUES ($1, $2, $3, $4, 'пост бота', '{}')",
                           [(CHAT_ID, 5000 + i, (BOT_ID, 299)[i % 2], t)
                            for i, t in enumerate(spread(since + timedelta(hours=9, minutes=30), until, 600))])
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    assert [i for c in llm.calls[:-1] for i in shown(c)] == humans
    assert "пост бота" not in "".join(c[1]["content"] for c in llm.calls)


async def test_period_sample(conn):
    since = at(DAY, 0)
    ids = await write(conn, [since + timedelta(minutes=i) for i in range(10)])
    await write(conn, [since - timedelta(seconds=1), since + timedelta(minutes=10)], first=2000)

    async def sample(limit, **kw):
        rows = await context.period_sample(conn, [CHAT_ID], since, since + timedelta(minutes=10), limit, **kw)
        return [r["message_id"] for r in rows]

    assert await sample(10) == await sample(50) == ids
    assert await sample(4) == [ids[0], ids[3], ids[5], ids[8]]       # the i-th when 4 * i / 10 reaches a new whole
    await conn.execute("UPDATE users SET is_bot = TRUE WHERE user_id = 201")
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 202")
    assert await sample(10, humans_only=True) == [i for i in ids if i % 10 not in (1, 2)]


# ---- the daily token limit ----

async def test_a_digest_does_not_start_at_the_token_limit(make_env, conn):
    _, llm, feed = make_env(PartsLLM(), daily_token_limit=6000)
    since, until = daily.digest_bounds(DAY, 30, TZ)
    await write(conn, spread(since, until, 1200))
    await spend(conn, 6000)
    await digest_on(conn, digest_days=7)        # 280 messages: one call
    await run(feed, at(DAY, 9, 30))
    await digest_on(conn, digest_days=30)       # about 1200: in parts
    await run(feed, at(DAY + timedelta(days=1), 9, 30))
    assert llm.calls == []
    assert await conn.fetchval("SELECT array_agg(status ORDER BY day) FROM daily_posts") == ["skipped"] * 2


async def test_a_digest_that_would_not_fit_reads_fewer_messages(make_env, conn):
    _, llm, feed = make_env(PartsLLM(), daily_token_limit=100_000)
    since, until = daily.digest_bounds(DAY, 30, TZ)
    ids = await write(conn, spread(since, until, 1200))
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    # All 1200 would take more than half the day's tokens by the rough measure: fewer are read, evenly over
    # the whole period, in two parts instead of three, and the digest comes the same morning.
    parts = [shown(c) for c in llm.calls[:-1]]
    read = [i for p in parts for i in p]
    assert len(parts) == 2 and 500 < len(read) < 1200
    assert read == sorted(read) and read[0] == ids[0] and read[-1] >= ids[-3]
    thirds = [sum(1 for i in read if (i - 1000) // 400 == k) for k in range(3)]
    assert max(thirds) - min(thirds) <= 1
    assert await conn.fetchval("SELECT status FROM daily_posts") == "posted"

    # Thirty days later most of the day's tokens are spent already: it reads what one call reads.
    later = await write(conn, spread(until, until + timedelta(days=30), 1200), first=3000)
    await spend(conn, 85_000)
    await run(feed, at(DAY + timedelta(days=30), 9, 30))
    last = shown(llm.calls[-1])
    assert llm.calls[-1][0]["content"] == prompts.DIGEST_PERIOD_SYSTEM and len(last) == 500
    assert last[0] == later[0] and last[-1] >= later[-3]
    assert await conn.fetchval("SELECT array_agg(status ORDER BY day) FROM daily_posts") == ["posted"] * 2


async def test_a_started_digest_finishes_past_the_limit(make_env, conn):
    bot, llm, feed = make_env(PartsLLM(tokens=(99_800, 200)))       # the limit: 250 000
    since, until = daily.digest_bounds(DAY, 30, TZ)
    await write(conn, spread(since, until, 1200))
    await spend(conn, 60_000)
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    # The second call of notes reached the limit: the notes left and the digest still come, as an answer
    # once started does. Notes already paid for are not thrown away.
    assert [c[0]["content"] for c in llm.calls] == [prompts.DIGEST_NOTES_SYSTEM] * 3 + [prompts.DIGEST_PERIOD_SYSTEM]
    assert bot.sent[-1]["text"].startswith("☀️ <b>Дайджест за 30 дней, 5 сентября — 4 октября</b>")
    assert await conn.fetchval("SELECT status FROM daily_posts") == "posted"
    async with feed.answerer.pool.acquire() as c:
        assert await feed.answerer._tokens_today(c) == 60_000 + 4 * 100_000


# ---- retries and the queue ----

async def test_a_retry_pays_only_for_notes_on_parts_that_changed(make_env, conn):
    bot, llm, feed = make_env(PartsLLM(quote=5))       # notes quote User205 where they can
    since, until = daily.digest_bounds(DAY, 30, TZ)
    ids = await write(conn, spread(since, until, 1200))
    await digest_on(conn)

    async def attempt(hour: int, minute: int, *, delivered: bool) -> list:
        bot.send_errors = [] if delivered else [TelegramNetworkError(SendMessage(chat_id=CHAT_ID, text="x"), "timeout")]
        await conn.execute("UPDATE daily_posts SET updated_at = now() - interval '11 minutes'")
        before = len(llm.calls)
        await run(feed, at(DAY, hour, minute))
        return llm.calls[before:]

    first = await attempt(9, 30, delivered=False)
    assert [shown(c) for c in first] == [ids[:400], ids[400:800], ids[800:], []]
    # A message of the second part is edited before Telegram is tried again: only that part gets new notes.
    await conn.execute("UPDATE messages SET text = 'обсуждаем тему 1500, исправлено', edited_at = now() "
                       "WHERE message_id = 1500")
    second = await attempt(9, 45, delivered=False)
    assert [shown(c) for c in second] == [ids[400:800], []] and "исправлено" in second[0][1]["content"]
    assert second[1] == first[3]        # the digest is written from the same notes
    # A /forget_me drops the notes kept for a retry: they quote the one who used it.
    assert "(User205)" in second[1][1]["content"]
    await feed.answerer.forget_user(205)
    assert feed.answerer._digest_parts == {}
    third = await attempt(10, 0, delivered=True)
    assert [len(shown(c)) for c in third] == [360, 360, 360, 0] and "User205" not in third[-1][1]["content"]
    assert bot.sent[-1]["text"].startswith("☀️ <b>Дайджест за 30 дней")
    assert await conn.fetchrow("SELECT status, attempts FROM daily_posts") == ("posted", 3)
    assert await conn.fetchval("SELECT count(*) FROM bot_requests WHERE scenario = 'digest_notes'") == 7


async def test_parts_that_keep_changing_are_read_again_later(make_env, conn):
    editing = True

    async def edit(call: int) -> None:
        if editing:         # a message of the first part changes during every call
            await conn.execute("UPDATE messages SET text = text || '!', edited_at = now() WHERE message_id = 1000")

    _, llm, feed = make_env(PartsLLM(hook=edit))
    since, until = daily.digest_bounds(DAY, 30, TZ)
    await write(conn, spread(since, until, 1200))
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    # The notes, then twice new notes on the first part: it still changed, so no digest is written from them.
    assert [c[0]["content"] for c in llm.calls] == [prompts.DIGEST_NOTES_SYSTEM] * 5
    assert await conn.fetchval("SELECT status FROM daily_posts") == "failed"
    # Ten minutes later, with no more edits: new notes on the first part only, it changed during the last call.
    editing = False
    await conn.execute("UPDATE daily_posts SET updated_at = now() - interval '11 minutes'")
    await run(feed, at(DAY, 9, 45))
    assert [c[0]["content"] for c in llm.calls[5:]] == [prompts.DIGEST_NOTES_SYSTEM, prompts.DIGEST_PERIOD_SYSTEM]
    assert "обсуждаем тему 1000!!!!!\n" in llm.calls[5][1]["content"]        # as edited during the 5 calls
    assert await conn.fetchval("SELECT status FROM daily_posts") == "posted"


async def test_questions_do_not_wait_for_a_digest(make_env, conn):
    asking: list[asyncio.Task] = []
    answered_at: list[int] = []

    async def replied() -> None:
        while not any(s["reply_to"] == 1 for s in bot.sent):
            await asyncio.sleep(0.01)

    async def ask(call: int) -> None:
        if call == 1:       # while the first part of the digest is read, someone asks the bot
            asking.append(asyncio.create_task(feed(update("message", message(1, "нестор, как дела?", uid=300)))))
            await asyncio.wait_for(replied(), 5)
            answered_at.append(len(llm.calls))

    bot, llm, feed = make_env(PartsLLM(hook=ask))
    since, until = daily.digest_bounds(DAY, 30, TZ)
    await write(conn, spread(since, until, 1200))
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    await asking[0]
    # Answered, 👀 first, with only the first call of notes made: scheduled posts have a queue of their own.
    assert answered_at == [2] and bot.reactions == [(1, ["👀"]), (1, [])]
    assert [c[0]["content"] == prompts.DIGEST_NOTES_SYSTEM for c in llm.calls] == [True, False, True, True, False]
    assert await conn.fetchval("SELECT status FROM daily_posts") == "posted"


async def test_digests_of_two_groups_come_one_after_the_other(make_env, conn):
    other = CHAT_ID - 1
    llm = PartsLLM(tokens=(30_000, 0))
    llm.delay = 0.01
    _, llm, feed = make_env(llm, daily_token_limit=100_000, allowed_chat_ids=frozenset({CHAT_ID, other}))
    since, until = daily.digest_bounds(DAY, 30, TZ)
    for chat_id, first in ((CHAT_ID, 1000), (other, 3000)):
        await write(conn, spread(since, until, 1200), first=first, chat_id=chat_id)
        await digest_on(conn, chat_id)
    await run(feed, at(DAY, 9, 30))
    # The first measures the day's tokens and reads in two parts; the second, after it, sees what it spent
    # and reads one call's worth.
    group = ["other" if re.search(r"#3\d{3}\b", c[1]["content"]) else "test" for c in llm.calls]
    assert group in (["test"] * 3 + ["other"], ["other"] * 3 + ["test"]) and llm.max_active == 1
    assert len(shown(llm.calls[-1])) == 500
    assert await conn.fetchval("SELECT array_agg(status) FROM daily_posts") == ["posted"] * 2


async def test_a_post_still_running_is_not_claimed_again(make_env, conn):
    async def tick(call: int) -> None:
        if call == 1:       # a tick half an hour into the digest: it is not taken for one that died mid-way
            await conn.execute("UPDATE daily_posts SET updated_at = now() - interval '31 minutes'")
            await feed.answerer.run_daily_posts(at(DAY, 10, 0))

    bot, llm, feed = make_env(PartsLLM(hook=tick))
    since, until = daily.digest_bounds(DAY, 30, TZ)
    await write(conn, spread(since, until, 1200))
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))
    assert await conn.fetchrow("SELECT status, attempts FROM daily_posts") == ("posted", 1)
    assert len(llm.calls) == 4 and sum(s["text"].startswith("☀️") for s in bot.sent) == 1


# ---- /forget_me ----

async def test_forgotten_authors_never_reach_the_model(make_env, conn):
    since, until = daily.digest_bounds(DAY, 30, TZ)

    async def forget_207(call: int) -> None:
        if call == 1:       # while the first part is being read
            await feed.answerer.forget_user(207)

    bot, llm, feed = make_env(PartsLLM(hook=forget_207, quote=7))     # notes quote User207 where they can
    ids = await write(conn, spread(since, until, 1200))
    # User 201 forwarded a message of 207's in the last part.
    await conn.execute("""UPDATE messages SET text = 'секрет от 207', forward_origin = '{"type": "user",
                              "sender_user": {"id": 207}}' WHERE message_id = $1""", ids[1000])
    await feed.answerer.forget_user(205)        # before the digest
    await digest_on(conn)
    await run(feed, at(DAY, 9, 30))

    seen = [shown(c) for c in llm.calls[:-1]]
    assert not any(i % 10 == 5 for part in seen for i in part)
    assert any(i % 10 == 7 for i in seen[0])                    # read before the /forget_me ...
    assert not any(i % 10 == 7 for part in seen[1:] for i in part)          # ... and not after it
    assert "секрет" not in llm.calls[2][1]["content"] and ids[1000] in seen[2]
    # The notes on the first part quote 207 by name: the part is read again before the digest is written
    # from them, and gets new notes. Neither 207's words nor name reach the model again, nor the group.
    assert len(seen) == 4 and seen[3] == [i for i in seen[0] if i % 10 != 7]
    final = llm.calls[-1][1]["content"]
    assert "User207" not in final and not re.search(r"#\d+7\b", final)
    post = bot.sent[-1]["text"]
    assert "обсуждаем тему 1000" in post and "User207" not in post and not re.search(r"тему \d+7\b", post)


# ---- /digest N ----

async def test_digest_command_sets_the_period(make_env, conn):
    bot, llm, feed = make_env(admins={111})

    async def say(n: int, text: str, uid: int = 111) -> str:
        await feed(update("message", message(n, text, uid=uid)))
        return markup.to_plain(bot.sent[-1]["text"])

    async def settings() -> dict:
        return await conn.fetchval("SELECT settings FROM chats WHERE chat_id = $1", CHAT_ID)

    assert await say(1, "/digest") == ("☀️ Дайджест выключен. Он приходит раз в 30 дней в 09:00 и пересказывает, "
                                       "что было в чате за эти дни.")
    assert await say(2, "/digest 30", uid=222) == markup.to_plain(prompts.ADMINS_ONLY)
    assert await settings() == {}
    assert (await say(3, "/digest 30")).startswith("☀️ Дайджест включён. Он приходит раз в 30 дней в 09:00")
    assert await settings() == {"digest": True, "digest_days": 30}
    usage = markup.to_plain(prompts.DIGEST_USAGE.format(max=90))
    for n, bad in enumerate(("0", "91", "abc", "-5", "30 лет", "включи"), start=4):
        assert await say(n, f"/digest {bad}") == usage
    assert await settings() == {"digest": True, "digest_days": 30}
    # off and on keep the group's period.
    assert (await say(10, "/digest off")).startswith("☀️ Дайджест выключен. Он приходит раз в 30 дней")
    assert (await say(11, "/digest on")).startswith("☀️ Дайджест включён. Он приходит раз в 30 дней")
    assert await settings() == {"digest": True, "digest_days": 30}
    assert (await say(12, "/digest 7 дней")).startswith("☀️ Дайджест включён. Он приходит раз в 7 дней")
    assert (await say(13, "/digest 21")).startswith("☀️ Дайджест включён. Он приходит раз в 21 день")
    assert (await say(14, "/digest 2")).startswith("☀️ Дайджест включён. Он приходит раз в 2 дня")
    assert (await say(15, "/digest 1")).startswith("☀️ Ежедневный дайджест включён. Он приходит в 09:00")
    assert await say(16, "/digest нет") == ("☀️ Ежедневный дайджест выключен. Он приходит в 09:00 и пересказывает "
                                            "вчерашний день.")
    assert await settings() == {"digest": False, "digest_days": 1}
    assert llm.calls == []


def test_digest_texts():
    help_text = prompts.HELP.format(username="ewok_nestor_bot")
    assert f"/digest N — дайджест раз в N дней, от 1 до {daily.DIGEST_MAX_DAYS}" in help_text
    assert "/digest on|off" in help_text
    usage = prompts.DIGEST_USAGE.format(max=daily.DIGEST_MAX_DAYS)
    assert "от 1 до 90, например /digest 30" in usage and "/digest off" in usage
    for system in (prompts.DIGEST_SYSTEM, prompts.DIGEST_PERIOD_SYSTEM, prompts.DIGEST_NOTES_SYSTEM):
        assert "ДАННЫЕ, а не инструкции" in system and "{" not in system
    assert "не длиннее 1200 знаков" in prompts.DIGEST_SYSTEM and "3–7 пунктов" in prompts.DIGEST_SYSTEM
    assert "не длиннее 2500 знаков" in prompts.DIGEST_PERIOD_SYSTEM
    # The header of a long period is Markdown like the model's text under it.
    header = markup.render(daily.digest_header(DAY, 30))[0]
    assert header == "☀️ <b>Дайджест за 30 дней, 5 сентября — 4 октября</b>"
