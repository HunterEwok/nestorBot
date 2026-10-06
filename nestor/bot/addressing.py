"""Addressing in the person's own words, the owner's «гибрид В» of 2026-10-05.

The clear cases are decided by code for free (nestor.bot.triggers.detect): a command, an @mention,
a reply to the bot, the name at the start with a comma, or alone. In a group where an admin turned on
/address, the doubtful ones are candidates: the name elsewhere in the message with a sign of
an address («слушай, Нестор, а ты как думаешь?»), the name at the start without a comma or a question
(«нестор вчера тупил»), and the next message of someone the bot has just answered, whatever it says
(«а ты уверен?», «а на завтра» a minute later), but for a thanks or an «ок» that asks nothing more
(«супер, спасибо!»). A candidate, with the few messages before it and without names, goes
to the profile without tools for a yes or a no; a yes is answered as if the bot had been called by name
(a thanks that asks nothing gets a reaction). No profile, past the daily number of checks, or when the
day's tokens run low: no check, and no answer. The code never guesses yes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg

from . import context, prompts
from .llm import LLM, Completion, leading_json, sole_json
from .reactions import acknowledgement, eligible, thanks
from .triggers import (
    MARK,
    BotIdentity,
    Invocation,
    keyword_pattern,
    name_regex,
    speaks_to,
)

log = logging.getLogger("nestor.addressing")


@dataclass(frozen=True)
class AddressConfig:
    window_min: float = 5          # ADDRESS_WINDOW_MIN: after an answer, the person's «а ты?» may be to the bot
    context: int = 4               # ADDRESS_CONTEXT: messages before a candidate shown with it
    daily_checks: int = 150        # ADDRESS_DAILY_CHECKS: model calls per chat per day
    timeout_s: float = 20          # a check that takes longer is a no


def _keywords(me: BotIdentity, generic_keyword_enabled: bool) -> tuple[str, ...]:
    return me.keywords if generic_keyword_enabled else tuple(k for k in me.keywords if k != "бот")


def _set_off(text: str, start: int, end: int) -> bool:
    """The name at text[start:end] is said to someone: a comma before it («слушай, Нестор, а ...», «спасибо,
    нестор 🙏») or a mark after it, as at the start («Всем привет. Нестор, ...», «привет нестор!»). Talk about
    the bot may look the same («спроси у Нестора, он знает»): the model tells them apart."""
    return text[:start].rstrip().endswith(",") or MARK.match(text[end:].lstrip()) is not None


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
    model to decide; None when it is clearly not. window: it is the person's next message here
    after the bot answered them a moment ago, a candidate whatever it says, but for a thanks or an
    «ок» that asks nothing more («ок», «супер, спасибо!», «спасибо, выручил»: nothing to answer, the
    reactions may say it). Only a person's own words: no bots, channels, forwards or commands. A reply
    to someone else is said to them, unless it starts with the name («нестор переведи» under a friend's
    message)."""
    text = msg.get("text") or msg.get("caption") or ""
    if not eligible(msg):
        return None
    keywords = _keywords(me, generic_keyword_enabled)
    names = re.compile(name_regex(keywords), re.IGNORECASE) if keywords else None
    found = list(names.finditer(text)) if names else []
    if keywords and keyword_pattern(keywords).match(text):      # «нестор вчера тупил»: maybe about it
        return Invocation("address", strip_name(text, names))
    if _replies_to_someone(msg):
        return None
    if found and (speaks_to(text) or any(_set_off(text, m.start(), m.end()) for m in found)):
        return Invocation("address", strip_name(text, names))
    if window and acknowledgement(text) is None and not thanks(text):
        return Invocation("address", strip_name(text, names) if names else text.strip())
    return None


def strip_name(text: str, names: re.Pattern) -> str:
    """The request without the name at its start or end and the marks around it:
    «как думаешь, нестор?» -> «как думаешь?», «нестор-то вчера тупил» -> «вчера тупил»; a name in
    the middle stays."""
    text = re.sub(rf"^\s*(?:{names.pattern})(?:-то(?!\w))?[\s,:;!.…—–-]*", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"[\s,:;—–-]*(?:{names.pattern})(?=[\s?!.…]*$)", "", text, flags=re.IGNORECASE)
    return text.strip()


# The bot's answers and posts are shown by their kind, never word for word: the archive model writes them, and
# it names whomever it quotes («Прав Петя Петров (@petya): «…»», «📊 Активность за 7 дней: 1. Петя Петров
# (@petya)…»); the posts are long too. Only its remarks (scenario chime), written from the labels, and its
# fixed texts (/help) are shown as they are.
POSTS = {"stats": "статистика", "summary": "пересказ", "catchup": "что пропустил", "digest": "дайджест",
         "wisdom": "мудрость дня", "news": "новости"}
ANSWER = "ответ"        # any other answer
SHOWN = 300             # characters of a message the models read; a longer one is cut, and says so
CUT = " …[обрезано]"


async def bot_posts(conn, rows, bot_id: int) -> dict[tuple[int, int], str]:
    """Which of the bot's messages among the rows are its answers and posts: (chat_id, message_id) -> the
    scenario of its request, or the kind of a daily post (the news, the same for every group, has no
    request of the group's own)."""
    ours = [r for r in rows if r["user_id"] == bot_id]
    if not ours:
        return {}
    found = await conn.fetch(
        """SELECT b.chat_id, a.id, b.scenario FROM bot_requests b CROSS JOIN unnest(b.answer_msg_ids) AS a(id)
           WHERE b.answer_msg_ids && $2::bigint[] AND b.scenario <> 'chime' AND b.chat_id = ANY($1::bigint[])
             AND a.id = ANY($2::bigint[])
           UNION ALL
           SELECT chat_id, message_id, kind FROM daily_posts
           WHERE chat_id = ANY($1::bigint[]) AND message_id = ANY($2::bigint[])""",
        list({r["chat_id"] for r in ours}), [r["message_id"] for r in ours])
    return {(f["chat_id"], f["id"]): f["scenario"] for f in found}


def render(rows, bot_id: int, *, posts: dict[tuple[int, int], str] | None = None, whole_last: bool = False) -> str:
    """The messages, numbered, oldest first, a label instead of each author: «Аноним 1», ..., and
    «Нестор» for the bot (labels no remark would say by chance: nestor.bot.chime refuses any that does).
    A reply to one of them says which. Each is cut to SHOWN characters, with «…[обрезано]» after it;
    whole_last: the last one, the message asked about, is shown whole (an address often ends a long
    message). posts: the bot's answers and posts among them (bot_posts), shown by their kind only. Messages
    of a forum's several topics say which one each is in, «[раздел 1]», ..., and «[общий раздел]» for
    General. A forward says so, «Аноним 2 переслал: …»: the words are someone else's. A photo, a voice
    message, a sticker are their kind, «[voice]», a caption comes after it: «[photo] …»."""
    labels: dict[int | None, str] = {}
    numbers = {(r["chat_id"], r["message_id"]): i for i, r in enumerate(rows, 1)}
    topics = [_row_topic(r) for r in rows]
    topic_labels = {t: f"раздел {i}" for i, t in enumerate(dict.fromkeys(t for t in topics if t is not None), 1)}
    lines = []
    for i, r in enumerate(rows, 1):
        who = "Нестор" if r["user_id"] == bot_id else labels.setdefault(r["user_id"], f"Аноним {len(labels) + 1}")
        topic = f"[{topic_labels.get(topics[i - 1], 'общий раздел')}] " if len(set(topics)) > 1 else ""
        target = numbers.get((r["chat_id"], r["reply_to_id"]))
        reply = f" (ответ на {target})" if target else ""
        post = (posts or {}).get((r["chat_id"], r["message_id"])) if r["user_id"] == bot_id else None
        text = f"[{POSTS.get(post, ANSWER)}]" if post else r["text"] or f"[{r['kind']}]"
        if not post and r["text"] and r["kind"] != "text":
            text = f"[{r['kind']}] {text}"
        forwarded = " переслал" if r.get("forwarded") else ""
        if len(text) > SHOWN and not (whole_last and i == len(rows)):
            text = text[:SHOWN] + CUT
        text = text.replace("\n", " ⏎ ")
        lines.append(f"{i}. {topic}{who}{forwarded}{reply}: {text}")
    return "\n".join(lines)


def _row_topic(row) -> int | None:
    """The forum topic of an archive row; None outside the topics (General, or a group without them)."""
    return row.get("thread_id") if row.get("is_topic_message") else None


def verdict(answer: str | None, *, strict: bool = True) -> bool | None:
    """The model's yes or no: {"to_bot": true} or {"to_bot": false}; None for anything else, a «да» in words,
    words before the object or two objects (an object in the answer may be quoted from the chat).
    strict: the object alone (sole_json), for a chat with a «{» in it, where an object may be planted;
    else words after it are its reasons (leading_json), «{"to_bot": true} Автор зовёт Нестора по имени».
    No verdict is a no."""
    data = sole_json(answer) if strict else leading_json(answer)
    value = data.get("to_bot") if isinstance(data, dict) else None
    return value if isinstance(value, bool) else None


def parse(answer: str | None, *, strict: bool = True) -> bool:
    """Whether the answer is the model's yes (verdict)."""
    return verdict(answer, strict=strict) is True


def _braced(text: str) -> bool:
    """Whether the text has a curly brace, «{» or a look-alike a model may type back as one: «｛», «❴», «⦃»."""
    return any("CURLY BRACKET" in unicodedata.name(ch, "") for ch in set(text))


Wanted = Callable[[int], Awaitable[bool]]
ChecksToday = Callable[[int], Awaitable[int]]
MaySpend = Callable[[int], Awaitable[bool]]     # given the checks of all chats still running
LogCall = Callable[[int, Completion, float, str], Awaitable[None]]
Answer = Callable[[dict, Invocation, int], Awaitable[None]]
Declined = Callable[[dict], None]


def _topic(msg: dict) -> int | None:
    return msg.get("message_thread_id") if msg.get("is_topic_message") else None


def _as_sent(row, msg: dict) -> bool:
    """Whether the archived candidate still says what it said when it came. A yes is answered as sent (the
    answer, as to any call, is to the message that came), so an edit before or during the check («ой, это я
    не тебе», a new question) is silence: the model's verdict would be on other words than the answer's."""
    return row["text"] == msg.get("text", msg.get("caption"))


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
        # One check at a time counts the day's checks and tokens: those of all chats still running count too,
        # their tokens not written down yet.
        self._counting = asyncio.Lock()
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

    def next_message(self, msg: dict) -> bool:
        """A message of a person here: whether it is their next one within the window after the bot's last
        answer to them (Telegram's dates are whole seconds: a second's slack). Whatever it is, a call by name,
        a command, «спасибо» or a word to a friend, the window after that answer closes with it; one written
        before the answer came leaves it open."""
        key, date = (msg["chat"]["id"], _topic(msg), msg["from"]["id"]), msg.get("date", 0)
        at = self._answered.get(key)
        if at is None or date < at - 1:
            return False
        del self._answered[key]
        return date <= at + self.cfg.window_min * 60

    # ---- candidates ----

    def consider(self, msg: dict, *, generic_keyword_enabled: bool, window: bool, order: int) -> bool:
        """A new stored message detect() did not take, in a group with /address on that is not
        paused: check it if it is a candidate. window: it is its author's next message after an answer
        (next_message). order: when it came (answerer.Job.order), so that a /forget_me of its author
        during the check still drops the answer."""
        if self.llm() is None:      # no profile without tools: candidates are ignored, never guessed
            return False
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
        async with self._counting:
            if await self.checks_today(chat_id) + self._in_flight.get(chat_id, 0) >= self.cfg.daily_checks:
                skipped = "daily_checks"
            elif not await self.may_spend(sum(self._in_flight.values())):
                skipped = "daily_tokens"
            else:
                skipped = None
                self._in_flight[chat_id] = self._in_flight.get(chat_id, 0) + 1
        if skipped:
            # Nothing is asked and nothing written down: the journal says why, so the silence shows.
            log.info("address check skipped", extra={"chat_id": chat_id, "reason": skipped})
            return False
        try:
            yes = await self._ask(llm, msg, window)
        finally:
            self._in_flight[chat_id] -= 1
        if not yes:
            return False
        # The model may take a while: a /pause, /address off, a deletion, an edit or the author's /forget_me
        # meanwhile still counts.
        if not await self.wanted(chat_id):
            return False
        async with self.pool.acquire() as conn:
            found = await context.messages_by_id(conn, chat_id, [msg["message_id"]])
        return bool(found) and _as_sent(found[0], msg)

    async def _ask(self, llm: LLM, msg: dict, window: bool = False) -> bool | None:
        """The model's verdict on the candidate, its call written down: status ok, or unparsed for an answer
        that is no verdict (neither the answer nor the verdict is kept). None when nothing was asked, the call
        failed or the answer is no verdict. window: a line says the bot has just answered its author (no
        words of the chat: the bot's answer and the request are often further back than the messages shown)."""
        chat_id = msg["chat"]["id"]
        async with self.pool.acquire() as conn:
            rows = await self._shown(conn, msg)
            posts = await bot_posts(conn, rows, self.me.user_id)
        if not rows:
            return None
        note = prompts.ADDRESS_WINDOW.format(n=len(rows), minutes=f"{self.cfg.window_min:g}") if window else ""
        shown = render(rows, self.me.user_id, posts=posts, whole_last=True)
        user = prompts.ADDRESS_USER.format(messages=shown, window=note, n=len(rows))
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
        # The reasons a model may add after its verdict are read as such only while nothing in the chat
        # looks like an object it might have quoted.
        try:
            yes = verdict(completion.text, strict=_braced(shown))
        except Exception:  # noqa: BLE001 - whatever the answer, the paid call is written down
            yes = None
        await self.log_call(chat_id, completion, started, "ok" if yes is not None else "unparsed")
        return yes

    async def _shown(self, conn, msg: dict) -> list:
        """The candidate read back from the archive and the ADDRESS_CONTEXT messages before it in
        its chat and topic, oldest first; nothing when the candidate is gone (deleted, its author's
        /forget_me) or edited since it came. Messages of people who used /forget_me are not among them."""
        chat_id = msg["chat"]["id"]
        found = await context.messages_by_id(conn, chat_id, [msg["message_id"]])
        if not found or not _as_sent(found[0], msg):
            return []
        ids = await context.chat_lineage(conn, chat_id)
        before = await context.before(conn, ids, _topic(msg), found[0], self.cfg.context) if self.cfg.context else []
        return [*before, found[0]]
