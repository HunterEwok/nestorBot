"""Addressing in the person's own words, the owner's «гибрид В» of 2026-10-05.

The clear cases are decided by code for free (nestor.bot.triggers.detect): a command, an @mention,
a reply to the bot, the name at the start with a comma, or alone. In a group where an admin turned on
/address, the doubtful ones are candidates: the name elsewhere in the message with a sign of
an address («слушай, Нестор, а ты как думаешь?»), the name at the start without a comma
(«нестор вчера тупил»), and the next message of someone the bot has just answered, with «ты»,
a request or a question («а ты уверен?» a minute later). A candidate, with the few messages
before it and without names, goes to the profile without tools for a yes or a no; a yes is
answered as if the bot had been called by name. No profile, past the daily number of checks, or
when the day's tokens run low: no check, and no answer. The code never guesses yes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg

from . import context, prompts
from .llm import LLM, Completion, sole_json
from .reactions import acknowledgement, eligible
from .triggers import BotIdentity, Invocation, keyword_pattern, name_forms, speaks_to

log = logging.getLogger("nestor.addressing")


@dataclass(frozen=True)
class AddressConfig:
    window_min: float = 5          # ADDRESS_WINDOW_MIN: after an answer, the person's «а ты?» may be to the bot
    context: int = 4               # ADDRESS_CONTEXT: messages before a candidate shown with it
    daily_checks: int = 150        # ADDRESS_DAILY_CHECKS: model calls per chat per day
    timeout_s: float = 20          # a check that takes longer is a no


# «а почему?», «и что дальше»: going on with the conversation. «а, круто», «а, точно»: a reaction to it.
_GOES_ON = re.compile(r"^\s*(?:а|и)(?!\w)(?!\s*,)", re.IGNORECASE)


def _keywords(me: BotIdentity, generic_keyword_enabled: bool) -> tuple[str, ...]:
    return me.keywords if generic_keyword_enabled else tuple(k for k in me.keywords if k != "бот")


def _set_off(text: str, start: int, end: int) -> bool:
    """The name at text[start:end] is said to someone: set off by commas («слушай, Нестор, а ...»,
    «спасибо, нестор», «спасибо, нестор 🙏»), or at the very end before «?» or «!» («привет нестор!»)."""
    before, after = text[:start].rstrip(), text[end:].lstrip()
    if re.fullmatch(r"[?!]+\W*", after):
        return True
    return (before.endswith(",") and (not after or after[0] in ",.!?…" or not re.search(r"\w", after))) or \
        (after.startswith(",") and (not before or before[-1] in ".!?…"))


def _replies_to_someone(msg: dict) -> bool:
    """A reply to another person's message: then it is said to them. Replies to the forum topic
    itself, to one's own message, to a channel post or to a bot are not."""
    reply = msg.get("reply_to_message")
    if not reply or (msg.get("is_topic_message") and reply.get("message_id") == msg.get("message_thread_id")):
        return False
    author = reply.get("from") or {}
    human = bool(author) and not author.get("is_bot") and "sender_chat" not in reply
    return human and author.get("id") != (msg.get("from") or {}).get("id")


def candidate(msg: dict, me: BotIdentity, *, generic_keyword_enabled: bool = True,
              window: bool = False) -> Invocation | None:
    """Whether a message that detect() did not take may still be addressed to the bot, for the
    model to decide; None when it is clearly not. window: the bot answered this person here a
    moment ago. Only a person's own words: no bots, channels, forwards or commands. A reply to
    someone else is said to them, unless it starts with the name («нестор кто прав» under a
    friend's message)."""
    text = msg.get("text") or msg.get("caption") or ""
    if not eligible(msg):
        return None
    keywords = _keywords(me, generic_keyword_enabled)
    names = re.compile(rf"(?<!\w)(?:{name_forms(keywords)})(?!\w)", re.IGNORECASE) if keywords else None
    found = list(names.finditer(text)) if names else []
    if keywords and keyword_pattern(keywords).match(text):      # «нестор вчера тупил»: maybe about it
        return Invocation("address", strip_name(text, names))
    if _replies_to_someone(msg):
        return None
    if found and (speaks_to(text) or any(_set_off(text, m.start(), m.end()) for m in found)):
        return Invocation("address", strip_name(text, names))
    # «а, понятно», «и тебе спасибо» right after an answer: a thanks, not a request. «а, круто» asks nothing.
    if window and (speaks_to(text) or _GOES_ON.match(text)) and acknowledgement(text) is None:
        return Invocation("address", strip_name(text, names) if names else text.strip())
    return None


def strip_name(text: str, names: re.Pattern) -> str:
    """The request without the name at its start or end and the marks around it:
    «как думаешь, нестор?» -> «как думаешь?», «нестор-то вчера тупил» -> «вчера тупил»; a name in
    the middle stays."""
    text = re.sub(rf"^\s*(?:{names.pattern})(?:-то(?!\w))?[\s,:;!.…—–-]*", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"[\s,:;—–-]*(?:{names.pattern})(?=[\s?!.…]*$)", "", text, flags=re.IGNORECASE)
    return text.strip()


# The bot's posts that are not a conversation, shown by their kind and never word for word: they
# name people («📊 Активность за 7 дней: 1. Петя Петров (@petya)…») and are long.
POSTS = {"stats": "статистика", "summary": "пересказ", "catchup": "что пропустил", "digest": "дайджест",
         "wisdom": "мудрость дня", "news": "новости"}


async def bot_posts(conn, rows, bot_id: int) -> dict[tuple[int, int], str]:
    """Which of the bot's messages among the rows are such posts: (chat_id, message_id) -> scenario."""
    ours = [r for r in rows if r["user_id"] == bot_id]
    if not ours:
        return {}
    found = await conn.fetch(
        """SELECT b.chat_id, a.id, b.scenario FROM bot_requests b CROSS JOIN unnest(b.answer_msg_ids) AS a(id)
           WHERE b.answer_msg_ids && $3::bigint[] AND b.scenario = ANY($1::text[]) AND b.chat_id = ANY($2::bigint[])
             AND a.id = ANY($3::bigint[])""",
        list(POSTS), list({r["chat_id"] for r in ours}), [r["message_id"] for r in ours])
    return {(f["chat_id"], f["id"]): f["scenario"] for f in found}


def render(rows, bot_id: int, *, posts: dict[tuple[int, int], str] | None = None, whole_last: bool = False) -> str:
    """The messages, numbered, oldest first, a label instead of each author: «Участник 1», ..., and
    «Нестор» for the bot. A reply to one of them says which. Each is cut to 300 characters; whole_last:
    the last one, the message asked about, is shown whole (an address often ends a long message).
    posts: the bot's posts among them (bot_posts), shown by their kind only. Messages of a forum's
    several topics say which one each is in, «[тема 1]», ..., and «[общая тема]» for General."""
    labels: dict[int | None, str] = {}
    numbers = {(r["chat_id"], r["message_id"]): i for i, r in enumerate(rows, 1)}
    topics = [_row_topic(r) for r in rows]
    topic_labels = {t: f"тема {i}" for i, t in enumerate(dict.fromkeys(t for t in topics if t is not None), 1)}
    lines = []
    for i, r in enumerate(rows, 1):
        who = "Нестор" if r["user_id"] == bot_id else labels.setdefault(r["user_id"], f"Участник {len(labels) + 1}")
        topic = f"[{topic_labels.get(topics[i - 1], 'общая тема')}] " if len(set(topics)) > 1 else ""
        target = numbers.get((r["chat_id"], r["reply_to_id"]))
        reply = f" (ответ на {target})" if target else ""
        post = (posts or {}).get((r["chat_id"], r["message_id"])) if r["user_id"] == bot_id else None
        text = f"[{POSTS[post]}]" if post else r["text"] or f"[{r['kind']}]"
        text = (text if whole_last and i == len(rows) else text[:300]).replace("\n", " ⏎ ")
        lines.append(f"{i}. {topic}{who}{reply}: {text}")
    return "\n".join(lines)


def _row_topic(row) -> int | None:
    """The forum topic of an archive row; None outside the topics (General, or a group without them)."""
    return row.get("thread_id") if row.get("is_topic_message") else None


def parse(answer: str | None) -> bool:
    """{"to_bot": true} -> True; anything else, a «да» in words or an answer with two objects (one may be
    quoted from the chat) included, is a no."""
    data = sole_json(answer)
    return isinstance(data, dict) and data.get("to_bot") is True


Wanted = Callable[[int], Awaitable[bool]]
ChecksToday = Callable[[int], Awaitable[int]]
MaySpend = Callable[[], Awaitable[bool]]
LogCall = Callable[[int, Completion, float, str], Awaitable[None]]
Answer = Callable[[dict, Invocation, int], Awaitable[None]]
Declined = Callable[[dict], None]


def _topic(msg: dict) -> int | None:
    return msg.get("message_thread_id") if msg.get("is_topic_message") else None


class Addresser:
    """Keeps the conversation windows, finds the candidates and checks them with the model.
    answer(msg, inv, order) gets every yes; declined(msg) every candidate that was not one."""

    def __init__(self, pool: asyncpg.Pool, llm: Callable[[], LLM | None], me: BotIdentity, *, wanted: Wanted,
                 checks_today: ChecksToday, may_spend: MaySpend, log_call: LogCall, answer: Answer,
                 declined: Declined, cfg: AddressConfig | None = None):
        self.pool, self.llm, self.me, self.cfg = pool, llm, me, cfg or AddressConfig()
        self.wanted, self.checks_today, self.may_spend, self.log_call = wanted, checks_today, may_spend, log_call
        self.answer, self.declined = answer, declined
        self._answered: dict[tuple[int, int | None, int], float] = {}   # (chat, topic, person) -> unix time
        self._checks: dict[tuple[int, int], asyncio.Task] = {}          # (chat, person) -> the check in flight
        self._waiting: dict[tuple[int, int], tuple[dict, Invocation, int, bool]] = {}
        self._asking: set[tuple[int, int]] = set()      # (chat, message) of the candidates in flight or waiting
        self._counting: dict[int, asyncio.Lock] = {}    # by chat: one check at a time counts the day's checks
        self._in_flight: dict[int, int] = {}            # by chat: checks asked and not written down yet

    # ---- the conversation window ----

    def opened(self, chat_id: int, topic: int | None, user_id: int, at: float | None = None) -> None:
        """The bot answered this person here (a delivered answer to their request): their next message
        within ADDRESS_WINDOW_MIN minutes, «а ты?», may be to the bot. Kept in memory only."""
        now = time.time() if at is None else at
        span = self.cfg.window_min * 60
        for key in [k for k, t in self._answered.items() if now - t > span]:
            del self._answered[key]
        self._answered[(chat_id, topic, user_id)] = now

    def window(self, msg: dict) -> bool:
        """Whether the message was written within the window after the bot's last answer to its
        author here (Telegram's dates are whole seconds: a second's slack)."""
        at = self._answered.get((msg["chat"]["id"], _topic(msg), msg["from"]["id"]))
        return at is not None and at - 1 <= msg.get("date", 0) <= at + self.cfg.window_min * 60

    # ---- candidates ----

    def consider(self, msg: dict, *, generic_keyword_enabled: bool, order: int) -> bool:
        """A new stored message detect() did not take, in a group with /address on that is not
        paused: check it if it is a candidate. order: when it came (answerer.Job.order), so that a
        /forget_me of its author during the check still drops the answer."""
        if self.llm() is None:      # no profile without tools: candidates are ignored, never guessed
            return False
        window = self.window(msg)
        if window:      # only the person's next message: «понятно», or a word to a friend, closes it
            del self._answered[(msg["chat"]["id"], _topic(msg), msg["from"]["id"])]
        inv = candidate(msg, self.me, generic_keyword_enabled=generic_keyword_enabled, window=window)
        if inv is None:
            return False
        key = (msg["chat"]["id"], msg["from"]["id"])
        self._asking.add((msg["chat"]["id"], msg["message_id"]))
        if key in self._checks and not self._checks[key].done():
            # One check per person at a time; the newest of theirs waits for it, older ones are dropped.
            dropped = self._waiting.pop(key, None)
            if dropped is not None:
                self._asking.discard((key[0], dropped[0]["message_id"]))
                self.declined(dropped[0])
            self._waiting[key] = (msg, inv, order, window)
        else:
            self._checks[key] = asyncio.create_task(self._run(key, msg, inv, order, window))
        return True

    def asking(self, chat_id: int, message_id: int) -> bool:
        """Whether this message is being checked or waits for its check (a yes queues its answer first)."""
        return (chat_id, message_id) in self._asking

    async def drain(self) -> None:
        """Wait for the checks in flight and those waiting for them (tests, shutdown)."""
        while checks := [t for t in self._checks.values() if not t.done()]:
            await asyncio.gather(*checks, return_exceptions=True)

    async def _run(self, key: tuple[int, int], msg: dict, inv: Invocation, order: int, window: bool) -> None:
        try:
            yes = await self.check(msg, window=window)
            if yes:
                await self.answer(msg, inv, order)
            else:
                self.declined(msg)
        except Exception:  # a check is never worth a crash
            log.exception("address check failed", extra={"chat_id": key[0]})
        finally:
            self._asking.discard((key[0], msg["message_id"]))
            waiting = self._waiting.pop(key, None)
            if waiting is not None:
                self._checks[key] = asyncio.create_task(self._run(key, *waiting))
            else:
                self._checks.pop(key, None)

    async def check(self, msg: dict, *, window: bool = False) -> bool:
        """The model's yes: the candidate is addressed to the bot. No on any error or timeout. window: it
        came in the conversation window, and the model is told the bot has just answered its author."""
        chat_id = msg["chat"]["id"]
        llm = self.llm()
        if llm is None or not await self.wanted(chat_id):
            return False
        # Checks of different people run at once: those not written down yet count toward the day's too.
        async with self._counting.setdefault(chat_id, asyncio.Lock()):
            if await self.checks_today(chat_id) + self._in_flight.get(chat_id, 0) >= self.cfg.daily_checks \
                    or not await self.may_spend():
                return False
            self._in_flight[chat_id] = self._in_flight.get(chat_id, 0) + 1
        try:
            completion = await self._ask(llm, msg, window)
        finally:
            self._in_flight[chat_id] -= 1
        if completion is None or not parse(completion.text):
            return False
        # The model may take a while: a /pause, /address off, a deletion or the author's /forget_me
        # meanwhile still counts.
        if not await self.wanted(chat_id):
            return False
        async with self.pool.acquire() as conn:
            return bool(await context.messages_by_id(conn, chat_id, [msg["message_id"]]))

    async def _ask(self, llm: LLM, msg: dict, window: bool = False) -> Completion | None:
        """The model's answer on the candidate, written down; None when nothing was asked or the call failed.
        window: a line says the bot has just answered its author (no words of the chat: the bot's answer
        and the request are often further back than the messages shown)."""
        chat_id = msg["chat"]["id"]
        async with self.pool.acquire() as conn:
            rows = await self._shown(conn, msg)
            posts = await bot_posts(conn, rows, self.me.user_id)
        if not rows:
            return None
        note = prompts.ADDRESS_WINDOW.format(n=len(rows), minutes=f"{self.cfg.window_min:g}") if window else ""
        user = prompts.ADDRESS_USER.format(messages=render(rows, self.me.user_id, posts=posts, whole_last=True),
                                           window=note, n=len(rows))
        started = time.monotonic()
        try:
            completion = await asyncio.wait_for(llm.complete([{"role": "system", "content": prompts.ADDRESS_SYSTEM},
                                                              {"role": "user", "content": user}]),
                                                self.cfg.timeout_s)
        except Exception as e:  # noqa: BLE001 - Hermes down or slow: silence
            log.warning("address model failed", extra={"chat_id": chat_id, "error": type(e).__name__})
            # A failed call counts toward the day's checks too: a broken profile is not asked all day.
            await self.log_call(chat_id, Completion("", None, None, None), started,
                                "timeout" if isinstance(e, asyncio.TimeoutError) else "error")
            return None
        await self.log_call(chat_id, completion, started, "ok")
        return completion

    async def _shown(self, conn, msg: dict) -> list:
        """The candidate read back from the archive and the ADDRESS_CONTEXT messages before it in
        its chat and topic, oldest first; nothing when the candidate is gone (deleted, its author's
        /forget_me). Messages of people who used /forget_me are not among them."""
        chat_id = msg["chat"]["id"]
        found = await context.messages_by_id(conn, chat_id, [msg["message_id"]])
        if not found:
            return []
        ids = await context.chat_lineage(conn, chat_id)
        before = await context.before(conn, ids, _topic(msg), found[0], self.cfg.context) if self.cfg.context else []
        return [*before, found[0]]
