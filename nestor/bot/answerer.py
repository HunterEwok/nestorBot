"""Answers invocations: limits (FR-12), "thinking" signal (FR-13), one job per chat at a
time (FR-14), conversation continuation (FR-15), scenarios SC-1..SC-6, audit log.

Besides answers, the bot speaks on its own only when someone asked for it: reminders,
the group digest and the wisdom of the day (started by nestor.bot.scheduler), and the
/export file in a private chat. Scheduled posts wait in a queue of their own, so they never
hold up a group's answers.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import html
import itertools
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time as dtime, timedelta, timezone
from typing import Awaitable, Callable, Collection
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError)
from aiogram.types import (BufferedInputFile, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReactionTypeEmoji,
                           ReplyParameters)

from .. import forget, promises
from ..collector.app import dump
from ..watchdog.alerts import fmt_since, render_status
from . import context, daily, export, markup, music, prompts, reactions, reminders, routing
from .llm import LLM, Completion
from .markup import TELEGRAM_LIMIT, split_text
from .triggers import BotIdentity, Invocation, detect

log = logging.getLogger("nestor.answerer")

ANONYMOUS_ADMIN_ID = 1087968824  # GroupAnonymousBot: an admin posting as the group (one of forget.SHARED_IDS)
STATUS_STALE_AFTER = timedelta(minutes=15)     # the watchdog runs every 5 minutes
_OFF = ("off", "выкл", "0", "нет", "no")
_ORDER = itertools.count()      # orders requests and /forget_me commands (Answerer._overtaken)
_ON = ("on", "вкл", "да", "yes")
_DIGEST_DAYS = re.compile(r"(\d+)(?:\s*(?:д|дн|день|дня|дней))?")       # /digest 30, /digest 7 дней

# The group digest reads DIGEST_CHUNK messages per model call at most. The digest of one day reads
# the latest DIGEST_CHUNK of yesterday in one call, as it always did. A longer period with more is
# cut by time into consecutive parts of at most that many: the archive model writes short notes on
# each part, keeping the numbers of its key messages, and the digest is written from the notes.
# Every call is its own row of bot_requests, so the daily token limit counts them all. Beyond
# DIGEST_MAX_MESSAGES the period is thinned evenly over time (context.period_sample): 4 calls of
# notes and the final one. The digests of all groups together take half of the day's tokens at most
# (answers keep the rest), every call of notes included (_digest_room). Before its first call a
# digest is measured roughly, DIGEST_CHARS_PER_TOKEN characters of the rendered messages per token
# and DIGEST_CALL_TOKENS for each call's own prompt, answer and the agent's overhead, and thinned
# further to fit in what is left of that half and of the day. When not even one call's worth fits
# (DIGEST_CHUNK messages, or all when fewer), it is skipped and comes the next morning. A digest once
# started finishes, as an answer does (the limit is soft): the tokens never stop its measured notes
# or its final call (only /pause or /digest off do). New notes on parts that changed meanwhile were
# not measured, so each is made only while it fits in what is left; else the attempt fails and keeps
# its notes for a retry.
DIGEST_CHUNK = 500
DIGEST_MAX_MESSAGES = 2000
DIGEST_CHARS_PER_TOKEN = 2.5
DIGEST_CALL_TOKENS = 5000
DIGEST_ROUNDS = 3       # rounds of notes: the first, then twice at most on the parts that changed meanwhile
# The digests of all groups wait in one queue of their own and go one at a time: a digest read in
# parts takes several calls and must not hold up its group's answers, and two digests never measure
# the day's tokens at the same time. The wisdom and the news go in a queue per group (_posts), apart
# from its answers and from the digests.
_DIGESTS = "digests"
_QueueKey = int | str | tuple[str, int]     # a group's answers, _DIGESTS, or _posts(chat_id)


def _posts(chat_id: int) -> tuple[str, int]:
    return ("posts", chat_id)


@dataclass(frozen=True)
class AnswerConfig:
    rate_limit: int = 5                 # calls per user ...
    rate_window_min: int = 10           # ... per this many minutes
    daily_token_limit: int = 250_000    # all chats together; soft: a started answer finishes
    timeout_s: float = 120
    progress_after_s: float = 60
    retention_months: int = promises.RETENTION_MONTHS
    tz: ZoneInfo = ZoneInfo("Europe/Moscow")
    smart_timeout_s: float = 240        # songs and the wisdom: a bigger model and several web searches
    music_daily_limit: int = 10         # songs per day, all chats together
    music_player_app: str = ""          # short name of the player Mini App in BotFather; empty: none
    max_reminders: int = 20             # pending reminders per person and chat
    allowed_chat_ids: frozenset[int] = frozenset()   # scheduled posts go only to these chats
    wisdom_chat_ids: frozenset[int] = frozenset()    # wisdom of the day on by default; /wisdom on|off overrides
    digest_time: dtime = dtime(9, 0)
    digest_every_days: int = 30         # DIGEST_EVERY_DAYS; a group's /digest N overrides
    wisdom_time: dtime = dtime(21, 0)
    news_chat_ids: frozenset[int] = frozenset()      # news digest on by default; /news on|off overrides
    news_time: dtime = dtime(20, 0)
    news_max_topics: int = 10
    reaction_chat_ids: frozenset[int] = frozenset()  # reactions on by default; /reactions on|off overrides
    reactions: reactions.ReactionConfig = reactions.ReactionConfig()
    alert_chat_id: int | None = None    # ALERT_CHAT_ID: /status answers there only, to its admins; None: nowhere


@dataclass
class Job:
    msg: dict
    inv: Invocation
    scenario: str
    question: str
    since: datetime | None = None       # /summary period, «что я пропустил»
    think: bool = False                 # «нестор, подумай …»: the bigger model
    seen: dict[int, int | None] = field(default_factory=dict)   # messages shown to the model: id -> author
    quoted: set[int] = field(default_factory=set)   # people whose words the answer carries besides its refs
    # When the oldest message whose words the answer may carry entered the archive: the answer
    # leaves the archive with it (bot_requests.oldest_source_at, nestor.retention).
    sources_at: datetime | None = None
    order: int = field(default_factory=lambda: next(_ORDER))   # when it was asked

    def saw(self, rows=(), *, at: datetime | None = None) -> None:
        """The answer may carry the words of these archive rows, or of something that entered the
        archive at `at` (an earlier turn's own oldest_source_at)."""
        self.sources_at = _oldest([self.sources_at, at, *(r["stored_at"] for r in rows)])


@dataclass
class Outgoing:
    """What an answer sends. Plain text is split into Telegram-sized parts; a model answer
    (markdown) is formatted by nestor.bot.markup and split the same way; an HTML card
    (songs) goes as one message."""
    text: str
    completion: Completion | None = None
    status: str = "ok"
    html: bool = False
    preview_url: str | None = None
    keyboard: InlineKeyboardMarkup | None = None
    log_text: str | None = None         # for bot_requests.answer when the text is not plain
    markdown: bool = False
    refs: dict[int, markup.Ref] | None = None   # archive messages the answer refers to


@dataclass
class _DigestParts:
    """A digest read in parts, until it is posted: daily.claim retries a failed post of the same day,
    and the retry reads these parts again and pays only for notes on the ones that changed meanwhile."""
    day: date
    days: int
    parts: list[list]           # the rows of each part, as last read
    notes: dict[str, str] = field(default_factory=dict)     # sha256 of a rendered part -> the notes on it


SaveOutgoing = Callable[[dict], Awaitable[object]]


class Answerer:
    def __init__(self, bot: Bot, pool: asyncpg.Pool, llm: LLM, me: BotIdentity,
                 save_outgoing: SaveOutgoing, cfg: AnswerConfig = AnswerConfig(), web_llm: LLM | None = None,
                 smart_llm: LLM | None = None, checker: music.Checker | None = None, deep_llm: LLM | None = None,
                 news_llm: LLM | None = None, routes: dict[str, str] | None = None, news=None):
        self.bot, self.pool, self.llm, self.me, self.cfg = bot, pool, llm, me, cfg
        # Which profile does which task (nestor.bot.routing). Profiles with web search have no
        # archive tools: they see the asker's own words, plus the one replied message for
        # «проверь»/«включи», the wisdom themes and public news items, never the archive.
        self.router = routing.Router({"group": llm, "deep": deep_llm, "web": web_llm, "smart": smart_llm,
                                      "news": news_llm}, routes)
        self.news = news                # nestor.news.service.NewsService, None without the news digest
        self.checker = checker or music.HttpChecker()
        self.save_outgoing = save_outgoing
        self.reactor = reactions.Reactor(bot, pool, lambda: self.router.llm("react"), wanted=self._reactions_wanted,
                                         calls_today=self._reaction_calls_today, may_spend=self._may_spend_on_reactions,
                                         log_call=self._log_reaction, cfg=cfg.reactions)
        # A queue per group for its answers and one for its other scheduled posts, and _DIGESTS for the
        # digests of all groups.
        self._queues: dict[_QueueKey, asyncio.Queue[Callable[[], Awaitable[None]]]] = {}
        self._workers: dict[_QueueKey, asyncio.Task] = {}
        self._posting: set[tuple[int, str]] = set()     # (chat_id, kind) of the posts queued or running
        self._digest_parts: dict[int, _DigestParts] = {}    # by chat_id: a digest not posted yet
        self._forgotten: dict[int, int] = {}   # user_id -> when their last /forget_me came, as Job.order

    # ---- entry points, called by the collector ----

    async def on_message(self, msg: dict, *, paused: bool) -> None:
        """A new group message that was just stored."""
        async with self.pool.acquire() as conn:
            settings = await conn.fetchval("SELECT settings FROM chats WHERE chat_id = $1", msg["chat"]["id"]) or {}
        inv = detect(msg, self.me, generic_keyword_enabled=settings.get("keyword_bot", True))
        if "from" not in msg:
            return
        if inv is None:
            if not paused and self._reactions_on(msg["chat"]["id"], settings):
                self.reactor.consider(msg)
            return
        if paused and inv.command not in ("resume", "status", "forget_me", "privacy"):   # /forget_me never waits
            return
        if inv.kind == "command":
            await self._command(msg, inv)
            return
        if await self._acknowledged(msg, inv):    # «спасибо», «ок»: a reaction says it, no model needed
            return
        think, question = routing.split_think(inv.text)
        scenario = context.classify(question, reply_target=_reply_target(msg, inv) is not None)
        self.submit(Job(msg, inv, scenario, question, think=think))

    async def on_private(self, msg: dict) -> None:
        """Private chats are never archived. Only /export works there (also through the
        t.me/<bot>?start=export link); anything else gets a pointer to the groups."""
        if "from" not in msg or msg["from"].get("is_bot"):
            return
        inv = detect(msg, self.me)
        if inv is not None and inv.kind == "command" and (
                inv.command == "export" or (inv.command == "start" and inv.args == "export")):
            await self._export(msg, private=True)
        else:
            await self._say(msg, prompts.PRIVATE_HELP, save=False)

    def submit(self, job: Job) -> None:
        self._enqueue(job.msg["chat"]["id"], functools.partial(self._answer, job))

    def _enqueue(self, key: _QueueKey, run: Callable[[], Awaitable[None]]) -> None:
        """key: the chat_id of the group whose answer it is, _DIGESTS, or _posts(chat_id)."""
        queue = self._queues.setdefault(key, asyncio.Queue())
        queue.put_nowait(run)
        if key not in self._workers or self._workers[key].done():
            self._workers[key] = asyncio.create_task(self._worker(key))

    async def drain(self) -> None:
        """Wait until all queued jobs are done (tests, shutdown), in queues made meanwhile too."""
        joined = 0
        while joined < len(self._queues):
            joined = len(self._queues)
            for queue in list(self._queues.values()):
                await queue.join()
        await self.reactor.drain()

    async def _worker(self, key: _QueueKey) -> None:
        queue = self._queues[key]
        while not queue.empty():
            run = await queue.get()
            try:
                await run()
            except Exception:  # noqa: BLE001 - one failed job must not stop the queue
                log.exception("job failed", extra={"queue": key})
            finally:
                queue.task_done()

    # ---- commands ----

    async def _command(self, msg: dict, inv: Invocation) -> None:
        chat_id, user_id = msg["chat"]["id"], msg["from"]["id"]
        cmd = inv.command
        if cmd in ("help", "start"):
            await self._say(msg, prompts.HELP.format(username=self.me.username))
        elif cmd == "privacy":
            await self._say(msg, prompts.PRIVACY.format(retention=promises.RETENTION_MONTHS,
                                                        backup_weeks=promises.BACKUP_WEEKS))
        elif cmd in ("forget_me", "export") and _shared_sender(msg):
            await self._say(msg, prompts.PERSONAL_ONLY, save=False)
        elif cmd == "forget_me":
            count = await self.forget_user(user_id)
            await self._say(msg, prompts.FORGOTTEN.format(count=count, backup_weeks=promises.BACKUP_WEEKS), save=False)
        elif cmd == "stats":
            self.submit(Job(msg, inv, "stats", "/stats"))
        elif cmd == "summary":
            since = context.period_start(inv.args, datetime.now(timezone.utc), self.cfg.tz)
            question = f"Краткое содержание обсуждения {inv.args or 'за сегодня'}"
            self.submit(Job(msg, inv, "summary", question, since=since))
        elif cmd == "catchup":
            self.submit(Job(msg, inv, "catchup", inv.args))
        elif cmd == "export":
            await self._export(msg, private=False)
        elif cmd == "reminders":
            await self._list_reminders(msg)
        elif cmd == "unremind":
            await self._unremind(msg, inv.args)
        elif cmd == "status":
            await self._status(msg)
        elif cmd in ("pause", "resume", "bot_word", "digest", "wisdom", "news", "reactions"):
            if not await self._is_admin(msg):
                await self._say(msg, prompts.ADMINS_ONLY)
                return
            if cmd == "news" and inv.args.strip().lower() in ("status", "статус"):
                await self._say(msg, await self._news_status())
                return
            if cmd in ("digest", "wisdom", "news", "reactions"):
                await self._toggle(msg, cmd, inv.args)
                return
            async with self.pool.acquire() as conn:
                if cmd == "bot_word":
                    on = inv.args.strip().lower() not in _OFF
                    await conn.execute(
                        "UPDATE chats SET settings = settings || jsonb_build_object('keyword_bot', $2::bool) "
                        "WHERE chat_id = $1", chat_id, on)
                    await self._say(msg, prompts.BOT_WORD.format(state="включено" if on else "выключено"))
                    return
                await conn.execute("UPDATE chats SET enabled = $2 WHERE chat_id = $1", chat_id, cmd == "resume")
            await self._say(msg, prompts.RESUMED if cmd == "resume" else prompts.PAUSED)

    async def _status(self, msg: dict) -> None:
        """/status: what nestor.watchdog found on its last run (its snapshot in ops_state), rendered as the
        watchdog renders it. Only in the alert group and only for its admins; elsewhere the bot keeps quiet,
        as if it had no such command. The answer is not archived."""
        if self.cfg.alert_chat_id is None or msg["chat"]["id"] != self.cfg.alert_chat_id:
            return
        if not await self._is_admin(msg):
            await self._say(msg, prompts.ADMINS_ONLY)
            return
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value, updated_at, now() - updated_at > $1 AS stale FROM ops_state "
                                      "WHERE key = 'watchdog'", STATUS_STALE_AFTER)
        now = datetime.now(UTC)
        if row is None:
            text = prompts.STATUS_NEVER
        else:
            text = render_status(row["value"], now, self.cfg.tz)
            if row["stale"]:
                text += "\n" + prompts.STATUS_STALE.format(time=fmt_since(row["updated_at"], now, self.cfg.tz))
        await self._say(msg, text, save=False)

    async def _toggle(self, msg: dict, key: str, args: str) -> None:
        """/digest, /wisdom, /news, /reactions on|off; without an argument shows the current state.
        /digest N also sets the group's digest period, in days, and switches the digest on."""
        chat_id, arg = msg["chat"]["id"], args.strip().lower()
        change = {key: arg not in _OFF} if arg else {}
        if key == "digest" and arg:
            change = _digest_change(arg)
            if change is None:      # /digest 0, /digest 91, /digest abc: nothing changes
                await self._say(msg, prompts.DIGEST_USAGE.format(max=daily.DIGEST_MAX_DAYS))
                return
        async with self.pool.acquire() as conn:
            if change:
                await conn.execute("UPDATE chats SET settings = settings || $2::jsonb WHERE chat_id = $1",
                                   chat_id, change)
            settings = await conn.fetchval("SELECT settings FROM chats WHERE chat_id = $1", chat_id) or {}
        if key == "digest":
            on, days = bool(settings.get("digest", False)), self._digest_days(settings)
            state, when = "включён" if on else "выключен", self.cfg.digest_time.strftime("%H:%M")
            text = (prompts.DIGEST_STATE.format(state=state, time=when) if days == 1
                    else prompts.DIGEST_STATE_EVERY.format(state=state, every=daily.days_name(days), time=when))
        elif key == "news":
            on = self._news_on(chat_id, settings)
            text = prompts.NEWS_STATE.format(state="включена" if on else "выключена",
                                             time=self.cfg.news_time.strftime("%H:%M"), topics=self.cfg.news_max_topics)
        elif key == "reactions":
            on = self._reactions_on(chat_id, settings)
            text = prompts.REACTIONS_STATE.format(state="включены" if on else "выключены",
                                                  per_hour=self.cfg.reactions.per_hour)
        else:
            on = self._wisdom_on(chat_id, settings)
            text = prompts.WISDOM_STATE.format(state="включена" if on else "выключена",
                                               time=self.cfg.wisdom_time.strftime("%H:%M"))
        await self._say(msg, text)

    def _digest_days(self, settings: dict) -> int:
        """The group's digest period (/digest N), else DIGEST_EVERY_DAYS."""
        value = (settings or {}).get("digest_days")
        return value if type(value) is int and 1 <= value <= daily.DIGEST_MAX_DAYS else self.cfg.digest_every_days

    def _wisdom_on(self, chat_id: int, settings: dict) -> bool:
        value = (settings or {}).get("wisdom")
        return bool(value) if value is not None else chat_id in self.cfg.wisdom_chat_ids

    def _news_on(self, chat_id: int, settings: dict) -> bool:
        value = (settings or {}).get("news")
        return bool(value) if value is not None else chat_id in self.cfg.news_chat_ids

    def _reactions_on(self, chat_id: int, settings: dict) -> bool:
        value = (settings or {}).get("reactions")
        return bool(value) if value is not None else chat_id in self.cfg.reaction_chat_ids

    def _post_on(self, chat_id: int, kind: str, settings: dict) -> bool:
        """Whether the group has this scheduled post on."""
        if kind == "digest":
            return bool((settings or {}).get("digest"))
        if kind == "wisdom":
            return self._wisdom_on(chat_id, settings)
        return self.news is not None and self._news_on(chat_id, settings)

    async def _still_wanted(self, conn, chat_id: int, kind: str) -> bool:
        """Checked when a queued post starts and before each of a digest's calls: /pause, /<kind> off or
        a move to a supergroup since it was queued stop it."""
        row = await conn.fetchrow("SELECT enabled, migrated_to_chat_id, settings FROM chats WHERE chat_id = $1",
                                  chat_id)
        return (row is not None and row["enabled"] and row["migrated_to_chat_id"] is None
                and self._post_on(chat_id, kind, row["settings"] or {}))

    # ---- reactions (nestor.bot.reactions) ----

    async def _reactions_wanted(self, chat_id: int) -> bool:
        """Checked again when a batch is due: the chat may have been paused or switched off meanwhile."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT enabled, settings FROM chats WHERE chat_id = $1", chat_id)
        return row is not None and row["enabled"] and self._reactions_on(chat_id, row["settings"] or {})

    async def _reaction_calls_today(self, chat_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM bot_requests WHERE chat_id = $1 AND scenario = 'reaction' AND created_at >= $2",
                chat_id, self._day_start())

    async def _may_spend_on_reactions(self) -> bool:
        """Reactions stop asking the model at half the daily tokens: answers come first."""
        async with self.pool.acquire() as conn:
            return await self._tokens_today(conn) < self.cfg.daily_token_limit // 2

    async def _log_reaction(self, chat_id: int, completion: Completion, started: float, status: str) -> None:
        async with self.pool.acquire() as conn:
            await self._log_scheduled(conn, chat_id, "reaction", completion, [], started, status=status)

    async def _put_reaction(self, msg: dict, emoji: str) -> bool:
        try:
            await self.bot.set_message_reaction(msg["chat"]["id"], msg["message_id"],
                                                reaction=[ReactionTypeEmoji(emoji=emoji)])
        except TelegramAPIError as e:
            log.warning("reaction failed", extra={"chat_id": msg["chat"]["id"], "error": type(e).__name__})
            return False
        return True

    async def _acknowledged(self, msg: dict, inv: Invocation) -> bool:
        """Answer a bare «спасибо», «ок» or emoji with a reaction the chat allows. Not when it
        answers the bot's own question («Ты про Кино?» — «угу») or replies to someone else's
        message: then it is a request. False sends the message on to the model."""
        emoji = reactions.acknowledgement(inv.text)
        if emoji is None or _reply_target(msg, inv) is not None:
            return False
        replied = (msg.get("reply_to_message") or {}).get("text") or ""
        if inv.replied_to_bot is not None and "?" in _LINKS.sub("", replied):   # a «?q=» inside a link is not one
            return False
        allowed = await self.reactor.allowed(msg["chat"]["id"])
        emoji = next((e for e in (emoji, "👍", "👌", "❤") if e in allowed), None)
        return emoji is not None and await self._put_reaction(msg, emoji)

    async def _is_admin(self, msg: dict) -> bool:
        if msg["from"]["id"] == ANONYMOUS_ADMIN_ID and (msg.get("sender_chat") or {}).get("id") == msg["chat"]["id"]:
            return True
        try:
            member = await self.bot.get_chat_member(msg["chat"]["id"], msg["from"]["id"])
        except TelegramAPIError:
            return False
        return member.status in ("creator", "administrator")

    async def forget_user(self, user_id: int) -> int:
        """/forget_me: delete the user's messages and reminders everywhere and stop storing new ones.
        Their requests still queued or being answered store nothing of theirs either (_overtaken)."""
        self._forgotten[user_id] = next(_ORDER)
        async with self.pool.acquire() as conn, conn.transaction():
            count = await forget.forget(conn, user_id, self.me.user_id)
        self._digest_parts.clear()      # notes kept for a retry of a digest may quote them
        return count

    def _overtaken(self, job: Job) -> bool:
        """The asker used /forget_me after asking. forget() erased what was stored of theirs when it ran;
        this request must not store their question or a reminder after it."""
        return self._forgotten.get(job.msg["from"]["id"], -1) > job.order

    async def _scrub_answers(self, conn, user_id: int, request_id: int | None = None) -> None:
        await forget.scrub_answers(conn, user_id, self.me.user_id, request_id)

    async def _scrub_if_opted_out(self, conn, request_id: int, quoted: list[int] | None) -> None:
        await forget.scrub_if_opted_out(conn, request_id, quoted, self.me.user_id)

    # ---- reminders ----

    async def _set_reminder(self, job: Job) -> Outgoing:
        msg, now = job.msg, datetime.now(timezone.utc)
        try:
            parsed = reminders.parse(job.question, now, self.cfg.tz)
        except reminders.ReminderError as e:
            return Outgoing(str(e), html=True)
        if parsed is None:
            return Outgoing(prompts.REMINDER_NO_TIME, html=True)
        chat_id, user_id = msg["chat"]["id"], msg["from"]["id"]
        async with self.pool.acquire() as conn:
            if await reminders.count_pending(conn, chat_id, user_id) >= self.cfg.max_reminders:
                return Outgoing(prompts.REMINDERS_TOO_MANY.format(limit=self.cfg.max_reminders), html=True)
            rid = await reminders.create(conn, chat_id=chat_id, thread_id=_topic(msg), user_id=user_id,
                                         request_msg_id=msg["message_id"], text=parsed.text, due=parsed.due)
        job.quoted.add(user_id)         # the confirmation repeats their words: /forget_me removes it too
        return Outgoing(prompts.REMINDER_SET.format(when=reminders.format_due(parsed.due, self.cfg.tz, now),
                                                    about=html.escape(reminders.about(parsed.text)), id=rid),
                        html=True)

    async def _list_reminders(self, msg: dict) -> None:
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            rows = await reminders.pending_for(conn, msg["chat"]["id"], msg["from"]["id"])
        if not rows:
            await self._say(msg, prompts.REMINDERS_EMPTY, save=False)
            return
        items = [prompts.REMINDER_ITEM.format(id=r["id"], when=reminders.format_due(r["due_at"], self.cfg.tz, now),
                                              about=html.escape(reminders.about(r["text"]))) for r in rows]
        # Their own reminders, not archived: /forget_me deletes reminders. Many long ones go in
        # several messages, the header with the first and the hint with the last.
        chunks = markup.chunk_lines(items, TELEGRAM_LIMIT - len(prompts.REMINDERS_LIST))
        for i, chunk in enumerate(chunks):
            text = "\n".join(chunk)
            if len(chunks) == 1:
                text = prompts.REMINDERS_LIST.format(items=text)
            elif i == 0:
                text = prompts.REMINDERS_LIST_HEADER + "\n" + text
            elif i == len(chunks) - 1:
                text += "\n\n" + prompts.REMINDERS_LIST_FOOTER
            await self._say(msg, text, save=False)

    async def _unremind(self, msg: dict, args: str) -> None:
        m = re.search(r"\d+", args or "")
        if not m:
            await self._say(msg, prompts.UNREMIND_USAGE)
            return
        rid = int(m.group())
        async with self.pool.acquire() as conn:
            done = await reminders.cancel(conn, rid, msg["chat"]["id"], msg["from"]["id"])
        if not done and await self._is_admin(msg):  # admins may cancel anyone's reminder in their group
            async with self.pool.acquire() as conn:
                done = await reminders.cancel(conn, rid, msg["chat"]["id"], None)
        await self._say(msg, prompts.UNREMIND_DONE.format(id=rid) if done else prompts.UNREMIND_NOT_FOUND)

    async def send_due_reminders(self, now: datetime | None = None) -> int:
        """Called by the scheduler. A reminder is a reply to the request that set it."""
        now = now or datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            rows = await reminders.due(conn, now)
        delivered = 0
        for r in rows:
            name = " ".join(p for p in (r["first_name"], r["last_name"]) if p) or r["username"] or "участник"
            about = reminders.about(r["text"]) or prompts.REMINDER_AS_AGREED
            text = prompts.REMINDER_FIRE.format(
                mention=f'<a href="tg://user?id={r["user_id"]}">{html.escape(name)}</a>', about=html.escape(about))
            if now - r["due_at"] > timedelta(hours=1):
                text += prompts.REMINDER_LATE.format(when=reminders.format_due(r["due_at"], self.cfg.tz, now))
            try:
                await self.bot.send_message(
                    r["chat_id"], text, parse_mode="HTML", message_thread_id=r["thread_id"],
                    reply_parameters=ReplyParameters(message_id=r["request_msg_id"], allow_sending_without_reply=True),
                    link_preview_options=LinkPreviewOptions(is_disabled=True))
            except TelegramRetryAfter as e:  # flood control: wait as long as Telegram asks, not a failure
                log.warning("reminder postponed by flood control",
                            extra={"chat_id": r["chat_id"], "retry_after": e.retry_after})
                async with self.pool.acquire() as conn:
                    await reminders.postpone(conn, r["id"], now + timedelta(seconds=e.retry_after), failed=False)
                continue
            except (TelegramNetworkError, TelegramServerError) as e:
                failures = r["attempts"] + 1
                log.warning("reminder not sent, will retry",
                            extra={"chat_id": r["chat_id"], "error": type(e).__name__, "failures": failures})
                async with self.pool.acquire() as conn:
                    if failures >= reminders.MAX_FAILURES:
                        await reminders.drop(conn, r["id"])
                    else:
                        await reminders.postpone(conn, r["id"], now + reminders.backoff(failures), failed=True)
                continue
            except TelegramAPIError as e:  # removed from the chat, chat gone: no point in retrying
                log.warning("reminder dropped", extra={"chat_id": r["chat_id"], "error": type(e).__name__})
                async with self.pool.acquire() as conn:
                    await reminders.drop(conn, r["id"])
                continue
            async with self.pool.acquire() as conn:
                await reminders.mark_sent(conn, r["id"])
            delivered += 1      # not archived: it repeats their words, and /forget_me deletes reminders
        return delivered

    # ---- /export ----

    async def _export(self, msg: dict, *, private: bool) -> None:
        user = msg["from"]
        job = Job(msg, Invocation("command", "", "export"), "export", "/export")
        status = "ok"
        async with self.pool.acquire() as conn:
            recent = await conn.fetchval(
                "SELECT count(*) FROM bot_requests WHERE user_id = $1 AND scenario = 'export' AND status = 'ok' "
                "AND created_at > now() - interval '10 minutes'", user["id"])
            rows = [] if recent else await export.fetch(conn, user["id"])
        if recent:
            status = "rate_limited"
            await self._say(msg, prompts.EXPORT_TOO_OFTEN, save=not private)
        elif not rows:
            await self._say(msg, prompts.EXPORT_EMPTY, save=not private)
        else:
            now = datetime.now(timezone.utc)
            document = BufferedInputFile(export.render(rows, user, self.cfg.tz, now),
                                         filename=export.filename(now, self.cfg.tz))
            try:
                await self.bot.send_document(user["id"], document,
                                             caption=prompts.EXPORT_CAPTION.format(count=len(rows)))
            except (TelegramForbiddenError, TelegramBadRequest):
                status = "blocked"  # the person has not opened a private chat with the bot yet
                await self._say(msg, prompts.EXPORT_START.format(username=self.me.username), save=not private)
            except TelegramAPIError as e:
                status = "error"
                log.warning("export failed", extra={"chat_id": msg["chat"]["id"], "error": type(e).__name__})
                await self._say(msg, prompts.ERROR, save=not private)
            else:
                if not private:
                    await self._say(msg, prompts.EXPORT_SENT)
        async with self.pool.acquire() as conn:
            await self._log(conn, job, status)

    # ---- answering ----

    async def _limit_reason(self, conn, user_id: int) -> str | None:
        recent = await conn.fetchval(
            """SELECT count(*) FROM bot_requests
               WHERE user_id = $1 AND status <> 'rate_limited' AND created_at > now() - make_interval(mins => $2)""",
            user_id, self.cfg.rate_window_min)
        if recent >= self.cfg.rate_limit:
            return prompts.RATE_LIMITED_USER.format(limit=self.cfg.rate_limit, window=self.cfg.rate_window_min)
        if await self._tokens_today(conn) >= self.cfg.daily_token_limit:
            return prompts.RATE_LIMITED_DAY
        return None

    def _day_start(self) -> datetime:
        return datetime.now(self.cfg.tz).replace(hour=0, minute=0, second=0, microsecond=0)

    async def _tokens_today(self, conn) -> int:
        return await conn.fetchval(
            "SELECT coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) "
            "FROM bot_requests WHERE created_at >= $1", self._day_start())

    async def _digest_room(self, conn) -> tuple[int, int]:
        """Today's tokens, and how many of them the digests may still spend: the digests of all groups
        together, every call of notes and every attempt included, take half of the daily limit at most
        (answers keep the rest), and never more than the day has left."""
        spent, digests = await conn.fetchrow(
            "SELECT coalesce(sum(t), 0), coalesce(sum(t) FILTER (WHERE scenario IN ('digest', 'digest_notes')), 0) "
            "FROM (SELECT scenario, coalesce(input_tokens, 0) + coalesce(output_tokens, 0) AS t "
            "      FROM bot_requests WHERE created_at >= $1) r", self._day_start())
        limit = self.cfg.daily_token_limit
        return spent, min(limit // 2 - digests, limit - spent)

    async def _log(self, conn, job: Job, status: str, **fields) -> int:
        """Queued requests log holding forget.ERASE_LOCK: a /forget_me of the asker then waits for
        this row, or this row sees that it came."""
        msg = job.msg
        return await conn.fetchval(
            """INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, thread_id, scenario, question,
                                         answer, model, input_tokens, output_tokens, latency_ms, status,
                                         answer_msg_ids, parent_id, profile, quoted_user_ids, oldest_source_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                       (SELECT id FROM bot_requests WHERE id = $14),    -- the retention purge may have deleted it
                       $15, $16, least(now(), coalesce($17::timestamptz, now()))) RETURNING id""",
            msg["chat"]["id"], msg["message_id"], msg["from"]["id"], msg.get("message_thread_id"),
            job.scenario, None if self._overtaken(job) else job.question, fields.get("answer"), fields.get("model"),
            fields.get("input_tokens"), fields.get("output_tokens"), fields.get("latency_ms"), status,
            fields.get("answer_msg_ids"), fields.get("parent_id"), fields.get("profile"), fields.get("quoted_user_ids"),
            fields.get("oldest_source_at"),
        )

    async def _answer(self, job: Job) -> None:
        msg = job.msg
        if self._overtaken(job):        # asked, then /forget_me while it waited in the queue: dropped
            return
        async with self.pool.acquire() as conn:
            reason = await self._limit_reason(conn, msg["from"]["id"])
            if reason:
                async with conn.transaction():
                    await forget.erase_lock(conn)
                    await self._log(conn, job, "rate_limited")
        if reason:
            await self._say(msg, reason)
            return

        await self._react(msg, "👀")
        ticket = secrets.token_urlsafe(18)
        started = time.monotonic()
        status, fields = "ok", {}
        try:
            async with self.pool.acquire() as conn:
                parent = await self._parent(conn, job)
                # The asker's own words may come back in the answer (a reminder repeats them).
                job.saw(at=await context.stored_at(conn, msg["chat"]["id"], msg["message_id"]))
            if parent is not None:      # a follow-up may repeat what the earlier turns carried
                job.saw(at=parent["oldest_source_at"])
            fields["parent_id"] = parent["id"] if parent else None
            out = await self._produce(job, parent, ticket)
            status = out.status
            # Counted before sending: tokens spent stay spent even if delivery fails.
            fields.update(answer=out.log_text or out.text, quoted_user_ids=_quoted(out.refs, job.quoted))
            if out.completion is not None:
                c = out.completion
                fields.update(model=c.model, input_tokens=c.input_tokens, output_tokens=c.output_tokens,
                              profile=c.profile)
            sent = await self._deliver(msg, out)
            fields.update(answer_msg_ids=[m.message_id for m in sent if m is not None])
        except asyncio.TimeoutError:
            status = "timeout"
            await self._say(msg, prompts.TIMEOUT)
        except Exception as e:  # noqa: BLE001 - LLMError, network, Hermes down: one short apology
            status = "error"
            log.warning("llm call failed", extra={"chat_id": msg["chat"]["id"], "error": type(e).__name__})
            await self._say(msg, prompts.ERROR)
        finally:
            fields["latency_ms"] = int((time.monotonic() - started) * 1000)
            fields["oldest_source_at"] = job.sources_at
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM request_tickets WHERE ticket = $1 OR expires_at < now()", ticket)
                async with conn.transaction():
                    # It may erase, and the purge may delete the parent request meanwhile: the lock
                    # comes before the insert's row locks, and waits for a purge batch to commit. A
                    # /forget_me of the asker or of someone quoted waits for it, or has come before it.
                    await forget.erase_lock(conn)
                    rid = await self._log(conn, job, status, **fields)
                    await self._scrub_if_opted_out(conn, rid, fields.get("quoted_user_ids"))
                    if self._overtaken(job):    # a reminder set while their /forget_me ran
                        await conn.execute("DELETE FROM reminders WHERE chat_id = $1 AND request_msg_id = $2 "
                                           "AND user_id = $3", msg["chat"]["id"], msg["message_id"], msg["from"]["id"])
            await self._react(msg, None)
        log.info("answered", extra={"chat_id": msg["chat"]["id"], "scenario": job.scenario, "status": status,
                                    "latency_ms": fields["latency_ms"], "input_tokens": fields.get("input_tokens"),
                                    "output_tokens": fields.get("output_tokens")})

    async def _produce(self, job: Job, parent, ticket: str) -> Outgoing:
        """Route a request: scenarios without a model, the web profiles, or the archive model."""
        scenario = job.scenario
        followed = parent["scenario"] if parent else None
        if scenario == "reminder":
            return await self._set_reminder(job)
        if scenario == "stats":
            return await self._stats(job)
        if scenario == "music" or followed == "music":
            job.scenario = "music"
            return await self._music(job, parent)
        if scenario == "factcheck":
            return await self._factcheck(job)
        web_task = "think_web" if job.think else "web"
        if self.router.llm(web_task) is not None and (scenario == "web" or followed in ("web", "factcheck")):
            # Plain web question or a follow-up to a web answer: the archive model is not needed.
            return _out(await self._answer_from_web(job, None))
        if scenario == "catchup":
            nothing = await self._start_catchup(job)
            if nothing:
                return Outgoing(nothing, html=True)

        task = "think" if job.think else scenario if scenario in ("theses", "who_is_right") else "answer"
        async with self.pool.acquire() as conn:
            prompt = await self._build_prompt(conn, job, ticket, parent)
            # The ticket outlives the call: a bigger model has a longer timeout and reads the archive till the end.
            await conn.execute(
                "INSERT INTO request_tickets (ticket, chat_id, thread_id, expires_at) VALUES ($1, $2, $3, $4)",
                ticket, job.msg["chat"]["id"], _topic(job.msg),
                datetime.now(timezone.utc) + timedelta(seconds=self._timeout(task) + 30))
        completion = await self._complete_with_progress(job.msg, prompt, self.router.llm(task),
                                                        timeout=self._timeout(task))
        if prompts.WEB_MARKER in completion.text:
            return _out(await self._answer_from_web(job, completion))
        refs, authors, oldest = await self._refs(job.msg["chat"]["id"], completion.text, job.seen)
        job.quoted |= authors
        job.saw(at=oldest)
        return _out(completion, refs)

    async def _deliver(self, msg: dict, out: Outgoing) -> list[Message | None]:
        if out.html or out.keyboard or out.preview_url:
            plain = (out.log_text or markup.to_plain(out.text)) if out.html else None
            return [await self._reply(msg, out.text, html=out.html, preview_url=out.preview_url, keyboard=out.keyboard,
                                      plain=plain)]
        if out.markdown:
            try:
                parts = markup.render(out.text, out.refs, msg["chat"]["id"])
            except Exception as e:  # noqa: BLE001 - a formatting bug must not lose an answer already paid for
                log.warning("formatting failed, sending plain text", extra={"chat_id": msg["chat"]["id"],
                                                                         "error": type(e).__name__})
                return [await self._reply(msg, part) for part in split_text(out.text)]
            return [await self._reply(msg, part, html=True, plain=markup.to_plain(part)) for part in parts]
        return [await self._reply(msg, part) for part in split_text(out.text)]

    async def _refs(self, chat_id: int, text: str, seen: dict[int, int | None] | None = None
                    ) -> tuple[dict[int, markup.Ref], set[int], datetime | None]:
        """The archive messages an answer refers to ([«…»](#id), t.me/c links, a bare #id), with
        their links, a piece of their text and their author. Deleted messages and authors who used
        /forget_me are not found, so their references stay without a link or quote. In a group
        migrated from a basic group both chats number from 1: an id found in both is left as it
        is, it cannot be told which message the model meant. A bare #id the prompt did not show
        is not linked either («issue #1234» is a number; the archive tools show ids the prompt
        does not, so the model may still mean a message), but its author is recorded all the same:
        the words beside such a number are usually theirs.

        Also returns the authors whose words the answer carries without a usable reference, so
        that /forget_me still removes it: of such ambiguous ids, of messages gone since the prompt
        was built (a /forget_me in flight), of the forwards it refers to (the link names who
        forwarded them), and of the people quoted in the bot's own answers it refers to.

        And when the oldest of the messages it refers to entered the archive, ambiguous ones
        included (and for the bot's own answers, their own oldest_source_at): the answer leaves the
        archive with them."""
        ids = markup.ref_ids(text, chat_id)
        if not ids:
            return {}, set(), None
        linked = markup.linked_ids(text, chat_id)
        found: dict[int, list] = {}
        refs, authors, times = {}, set(), []
        async with self.pool.acquire() as conn:
            for cid in await context.chat_lineage(conn, chat_id):
                wanted = ids if cid == chat_id else [i for i in ids if i not in linked]
                for r in await context.messages_by_id(conn, cid, wanted):
                    found.setdefault(r["message_id"], []).append(r)
            for mid in ids:
                rows = found.get(mid, [])
                for r in rows:
                    times.append(r["stored_at"])
                    if r["forwarded_from"] is not None:     # a forward: the words are its author's
                        authors.add(r["forwarded_from"])
                    # The bot's answer: whoever it quoted is quoted again, and it carries what that one did.
                    if r["user_id"] == self.me.user_id:
                        req = await conn.fetchrow(
                            "SELECT quoted_user_ids, oldest_source_at FROM bot_requests "
                            "WHERE chat_id = $1 AND $2 = ANY(answer_msg_ids)", r["chat_id"], mid)
                        if req is not None:
                            authors |= set(req["quoted_user_ids"] or [])
                            times.append(req["oldest_source_at"])
                if len(rows) == 1:
                    r = rows[0]
                    own = r["user_id"] == self.me.user_id
                    refs[mid] = markup.Ref(
                        context.message_link(r["chat_id"], r["message_id"], r["thread_id"], r["is_topic_message"]),
                        markup.quote(r["text"]), None if own else r["user_id"],
                        bare=seen is None or mid in seen)       # None: no prompt was built (tests, imports)
                elif rows:
                    authors |= {r["user_id"] for r in rows if r["user_id"] is not None}
                elif (seen or {}).get(mid) is not None:
                    authors.add(seen[mid])
        return refs, authors - {self.me.user_id}, _oldest(times)

    async def _parent(self, conn, job: Job):
        """The earlier request whose answer this message replies to, if any."""
        if job.inv.replied_to_bot is None:
            return None
        return await conn.fetchrow(
            f"SELECT {_TURN} FROM bot_requests WHERE chat_id = $1 AND $2 = ANY(answer_msg_ids)",
            job.msg["chat"]["id"], job.inv.replied_to_bot)

    async def _start_catchup(self, job: Job) -> str | None:
        """«Что я пропустил»: the period starts at the asker's previous message here (at most a
        week back; a day if they never wrote). Returns a ready answer if nobody wrote since."""
        msg, tz = job.msg, self.cfg.tz
        now = datetime.now(timezone.utc)
        sent_at = datetime.fromtimestamp(msg["date"], timezone.utc)
        async with self.pool.acquire() as conn:
            ids = await context.chat_lineage(conn, msg["chat"]["id"])
            last = await context.last_message_at(conn, ids, _topic(msg), msg["from"]["id"], before=sent_at,
                                                 exclude_id=msg["message_id"])
            since = max(last, now - timedelta(days=7)) if last else now - timedelta(hours=24)
            count = await context.count_since(conn, ids, _topic(msg), since, except_user=msg["from"]["id"])
        local = since.astimezone(tz)
        when = local.strftime("%H:%M") if local.date() == now.astimezone(tz).date() else local.strftime("%d.%m %H:%M")
        if not count:
            return prompts.CATCHUP_NOTHING.format(since=when)
        job.since = since
        job.question = prompts.CATCHUP_QUESTION.format(since=when) + (f" {job.question}" if job.question else "")
        return None

    async def _replied_text(self, conn, msg: dict) -> tuple[str | None, bool]:
        """Text of the message the request replies to (the quoted part, if any) and whether its
        author opted out. Used only where the asker picked that message on purpose."""
        reply = _reply_target(msg)
        if reply is None:
            return None, False
        author = (reply.get("from") or {}).get("id")
        if author is not None and await conn.fetchval("SELECT opted_out FROM users WHERE user_id = $1", author):
            return None, True
        text = ((msg.get("quote") or {}).get("text") or reply.get("text") or reply.get("caption") or "").strip()
        return (text[:2000] or None), False

    async def _claim_of(self, conn, chat_id: int, trigger_msg_id: int | None) -> str | None:
        """For follow-ups: the text an earlier fact-check was about, read back from the archive,
        so /forget_me of its author also removes it from later conversations."""
        if trigger_msg_id is None:
            return None
        row = await conn.fetchrow(
            """SELECT t.raw->'quote'->>'text' AS quote, r.text, u.opted_out
               FROM messages t
               LEFT JOIN messages r ON r.chat_id = t.chat_id AND r.message_id = t.reply_to_id
               LEFT JOIN users u ON u.user_id = r.user_id
               WHERE t.chat_id = $1 AND t.message_id = $2""", chat_id, trigger_msg_id)
        if row is None or row["opted_out"]:
            return None
        return row["quote"] or row["text"]

    async def _factcheck(self, job: Job) -> Outgoing:
        """Reply «нестор, проверь» to a message: that one message goes to the web profile."""
        llm = self.router.llm("think_web" if job.think else "factcheck")
        if llm is None:
            return Outgoing(prompts.NO_WEB, html=True)
        if _reply_target(job.msg, job.inv) is None:
            return Outgoing(prompts.FACTCHECK_HOW, html=True)
        async with self.pool.acquire() as conn:
            claim, opted_out = await self._replied_text(conn, job.msg)
            if claim is not None:
                job.saw(at=await context.stored_at(conn, job.msg["chat"]["id"], _reply_target(job.msg)["message_id"]))
        if claim is None:
            return Outgoing(prompts.REPLIED_OPTED_OUT if opted_out else prompts.FACTCHECK_NO_TEXT, html=True)
        job.quoted.add(_replied_author(job.msg))    # the verdict restates their claim
        now = datetime.now(self.cfg.tz).strftime("%Y-%m-%d %H:%M")
        user = (f"Сейчас {now} ({self.cfg.tz.key}). Утверждение из чата: «{claim}»\n"
                f"Вопрос участника: {job.question or 'проверь'}")
        prompt = [{"role": "system", "content": prompts.FACTCHECK_SYSTEM}, {"role": "user", "content": user}]
        return _out(await self._complete_with_progress(job.msg, prompt, llm,
                                                       timeout=self._timeout("think_web" if job.think else "factcheck")))

    async def _music(self, job: Job, parent) -> Outgoing:
        """«нестор, включи …»: the smart profile finds the song, music.build_card checks the links."""
        llm = self.router.llm("music")
        if llm is None:
            return Outgoing(prompts.NO_WEB, html=True)
        extra = None
        async with self.pool.acquire() as conn:
            used = await conn.fetchval(
                "SELECT count(*) FROM bot_requests WHERE scenario = 'music' AND status = 'ok' AND created_at >= $1",
                self._day_start())
            if used >= self.cfg.music_daily_limit:
                return Outgoing(prompts.MUSIC_LIMIT.format(limit=self.cfg.music_daily_limit), status="rate_limited",
                                html=True)
            if parent is None and music.needs_reply_text(job.question):
                extra, opted_out = await self._replied_text(conn, job.msg)
                if extra is None:
                    return Outgoing(prompts.REPLIED_OPTED_OUT if opted_out else prompts.MUSIC_NO_TEXT, html=True)
                job.quoted.add(_replied_author(job.msg))
                job.saw(at=await context.stored_at(conn, job.msg["chat"]["id"], _reply_target(job.msg)["message_id"]))
            history = await self._history(conn, job, ("music",))
        user = f"Запрос: {job.question}"
        if extra:
            user += f"\nПесня из сообщения, на которое ответили: «{extra[:500]}»"
        prompt = [{"role": "system", "content": prompts.MUSIC_SYSTEM}, *history, {"role": "user", "content": user}]
        completion = await self._complete_with_progress(job.msg, prompt, llm, timeout=self._timeout("music"))
        pick = music.parse(completion.text)
        if pick is None:
            return Outgoing(prompts.MUSIC_NOT_FOUND, completion, html=True, log_text=completion.text)
        if isinstance(pick, music.Say):
            return Outgoing(pick.text, completion)
        card = await music.build_card(pick, self.checker,
                                      player=music.player_link(self.me.username, self.cfg.music_player_app))
        return Outgoing(card.html, completion, html=True, preview_url=card.preview_url, keyboard=card.keyboard,
                        log_text=card.plain)

    async def _stats(self, job: Job) -> Outgoing:
        """Activity over 7 days and the week's longest reply branches, named by the archive model."""
        msg, tz = job.msg, self.cfg.tz
        since = datetime.now(timezone.utc) - timedelta(days=7)
        async with self.pool.acquire() as conn:
            ids = await context.chat_lineage(conn, msg["chat"]["id"])
            activity = await conn.fetch(
                """SELECT u.first_name, u.last_name, u.username, count(*) AS n
                   FROM messages m JOIN users u ON u.user_id = m.user_id
                   WHERE m.chat_id = ANY($1::bigint[]) AND m.kind <> 'service' AND NOT u.is_bot
                     AND m.sent_at > $2
                   GROUP BY u.user_id ORDER BY n DESC LIMIT 10""", ids, since)
            if not activity:
                return Outgoing(prompts.STATS_EMPTY, html=True)
            found = await context.top_branches(conn, ids, None, since)
            branches = []
            for b in found:
                members = list(b["members"])
                rows = await context.messages_by_id(conn, b["chat_id"], [b["root"]] + members[:6] + members[-3:])
                if rows:
                    branches.append((b, rows))
                    job.saw(rows)       # the model names the branch from these, its name may be their words
        lines = [f"<b>{html.escape(prompts.STATS_ACTIVITY)}</b>"] + [
            f"{i}. {html.escape(context.author(r))} — {r['n']}" for i, r in enumerate(activity, 1)]
        if not branches:
            text = "\n".join(lines + ["", html.escape(prompts.STATS_NO_TOPICS)])
            return Outgoing(text, html=True, log_text=markup.to_plain(text))

        completion, names = None, []
        prompt = [{"role": "system", "content": prompts.TOPICS_SYSTEM},
                  {"role": "user", "content": "\n\n".join(
                      context.render(rows, tz, title=f"ветка {i}") for i, (_, rows) in enumerate(branches, 1))}]
        try:
            completion = await self._complete_with_progress(msg, prompt, self.router.llm("topics"),
                                                            timeout=self._timeout("topics"))
            names = _numbered(completion.text, len(branches))
        except Exception as e:  # noqa: BLE001 - the activity part is still worth sending
            log.warning("topic naming failed", extra={"chat_id": msg["chat"]["id"], "error": type(e).__name__})
        lines += ["", f"<b>{html.escape(prompts.STATS_TOPICS)}</b>"]
        for i, (b, rows) in enumerate(branches, 1):
            first = rows[0]
            name = names[i - 1] if i <= len(names) else None
            if name is None:        # the first words of the branch: its author's own
                name = _snippet(first["text"])
                if first["user_id"] is not None:
                    job.quoted.add(first["user_id"])
            else:                   # the model named it from these messages and may have kept their words
                job.quoted |= {r["user_id"] for r in rows if r["user_id"] not in (None, self.me.user_id)}
            name = html.escape(name)
            size = b["size"]
            link = context.message_link(first["chat_id"], first["message_id"], first["thread_id"],
                                        first["is_topic_message"])
            if link:
                name = f'<a href="{html.escape(link, quote=True)}">{name}</a>'
            lines.append(f"{i}. {name} — {size} {context.plural(size, ('сообщение', 'сообщения', 'сообщений'))}")
        text = "\n".join(lines)
        return Outgoing(text, completion, html=True, log_text=markup.to_plain(text))

    async def _answer_from_web(self, job: Job, routed: Completion | None) -> Completion:
        """The archive model asked for the internet. The web profile gets the asker's question and
        earlier web turns of this conversation only: no archive text can reach a search engine."""
        job.scenario = "web"
        task = "think_web" if job.think else "web"
        llm = self.router.llm(task)
        if llm is None or not job.question.strip():
            text = prompts.NO_WEB if llm is None else prompts.EMPTY_WEB_QUESTION
            return Completion(text, *((routed.model, routed.input_tokens, routed.output_tokens, routed.profile)
                                      if routed else (None, None, None, None)))
        async with self.pool.acquire() as conn:
            history = await self._history(conn, job, ("web", "factcheck"))
            style = await self._style(conn, job)
        now = datetime.now(self.cfg.tz).strftime("%Y-%m-%d %H:%M")
        prompt = [{"role": "system", "content": prompts.WEB_SYSTEM}, *history,
                  {"role": "user", "content": f"Сейчас {now} ({self.cfg.tz.key}). Вопрос: {job.question}"
                                              + (f"\n{style}" if style else "")}]
        web = await self._complete_with_progress(job.msg, prompt, llm, timeout=self._timeout(task))
        if routed is None:
            return web
        return Completion(web.text, web.model, _add(routed.input_tokens, web.input_tokens),
                          _add(routed.output_tokens, web.output_tokens), web.profile)

    async def _history(self, conn, job: Job, scenarios: tuple[str, ...]) -> list[dict]:
        """Earlier turns of a reply chain, as long as they belong to the given scenarios."""
        history: list[dict] = []
        parent = await self._parent(conn, job)
        while parent and parent["scenario"] in scenarios and parent["question"] and len(history) < 6:
            question = parent["question"]
            if parent["scenario"] == "factcheck":
                claim = await self._claim_of(conn, parent["chat_id"], parent["trigger_msg_id"])
                question = f"Утверждение из чата: «{claim or '(сообщение недоступно)'}»\nВопрос участника: {question}"
            history[:0] = [{"role": "user", "content": question},
                           {"role": "assistant", "content": parent["answer"] or "(ответ удалён)"}]
            job.quoted |= set(parent["quoted_user_ids"] or [])     # a follow-up may repeat the quotes
            job.saw(at=parent["oldest_source_at"])
            parent = await conn.fetchrow(f"SELECT {_TURN} FROM bot_requests WHERE id = $1",
                                         parent["parent_id"]) if parent["parent_id"] else None
        return history

    async def _complete_with_progress(self, msg: dict, prompt: list[dict], llm: LLM | None = None,
                                      timeout: float | None = None):
        total = timeout or self.cfg.timeout_s
        task = asyncio.ensure_future((llm or self.llm).complete(prompt))
        try:
            return await asyncio.wait_for(asyncio.shield(task), min(self.cfg.progress_after_s, total))
        except asyncio.TimeoutError:
            note = await self._say(msg, prompts.STILL_THINKING, save=False)
            try:
                return await asyncio.wait_for(task, max(total - self.cfg.progress_after_s, 0.01))
            finally:
                if note is not None:
                    try:
                        await self.bot.delete_message(msg["chat"]["id"], note.message_id)
                    except TelegramAPIError:
                        pass

    async def _style(self, conn, job: Job) -> str:
        """How long the asker usually writes here, as one line for the prompt: answers match it.
        Only for free questions: summaries, catch-ups, theses and searches keep their own format.
        Only a length, never chat text, so the web profiles may get it too."""
        if job.scenario not in ("other", "web"):
            return ""
        ids = await context.chat_lineage(conn, job.msg["chat"]["id"])
        return context.style_hint(await context.typical_length(conn, ids, job.msg["from"]["id"]), job.question)

    def _timeout(self, task: str) -> float:
        """Bigger models and several web searches take longer."""
        return self.cfg.smart_timeout_s if self.router.is_big(task) else self.cfg.timeout_s

    async def _build_prompt(self, conn, job: Job, ticket: str, parent) -> list[dict]:
        msg, tz = job.msg, self.cfg.tz
        chat_id = msg["chat"]["id"]
        ids = await context.chat_lineage(conn, chat_id)
        thread = _topic(msg)
        blocks: list[str] = []
        who = context.author({"first_name": msg["from"].get("first_name"), "last_name": msg["from"].get("last_name"),
                              "username": msg["from"].get("username")})

        def show(rows, title: str) -> str:
            """Rendered for the model, and remembered: these ids may be referred to, these words
            may be repeated."""
            job.seen.update({r["message_id"]: r["user_id"] for r in rows})
            job.saw(rows)
            return context.render(rows, tz, title=title)

        reply = msg.get("reply_to_message") or {}
        target = reply.get("message_id")
        if target and (msg.get("is_topic_message") and target == msg.get("message_thread_id")):
            target = None
        if target and job.inv.replied_to_bot is None:
            chain = await context.reply_chain(conn, chat_id, target)
            below = await context.replies_to(conn, chat_id, target)
            blocks.append(f"Запрос — ответ на сообщение #{target}; оно и его ветка:")
            blocks.append(show(chain + [r for r in below if r["message_id"] != msg["message_id"]], "ветка"))
            if _replied_author(msg) is not None:    # the answer is about their message: theses, who is right
                job.quoted.add(_replied_author(msg))

        for user in await context.mentioned_users(conn, msg, self.me.username):
            rows = await context.user_messages(conn, ids, thread, user["user_id"])
            blocks.append(show(rows, f"сообщения {context.author(user)} за сутки"))

        if job.since is not None:
            rows = await context.period_messages(conn, ids, thread, job.since, limit=500)
            blocks.append(show([r for r in rows if r["message_id"] != msg["message_id"]], "сообщения за период"))
            if job.scenario == "catchup":
                rows = await context.addressed_to(conn, ids, thread, msg["from"]["id"], msg["from"].get("username"),
                                                  job.since)
                blocks.append(show(rows, f"ответы и упоминания для {who}"))
        else:
            rows = await context.recent(conn, ids, thread)
            blocks.append(show([r for r in rows if r["message_id"] != msg["message_id"]], "последние сообщения чата"))

        history: list[dict] = []
        depth = 0
        while parent and depth < 3:
            history[:0] = [{"role": "user", "content": parent["question"] or "(вопрос удалён)"},
                           {"role": "assistant", "content": parent["answer"] or "(ответ удалён)"}]
            job.quoted |= set(parent["quoted_user_ids"] or [])     # a follow-up may repeat the quotes
            job.saw(at=parent["oldest_source_at"])
            parent = await conn.fetchrow(f"SELECT {_TURN} FROM bot_requests WHERE id = $1",
                                         parent["parent_id"]) if parent["parent_id"] else None
            depth += 1

        title = msg["chat"].get("title") or ""
        now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        question = job.question or ("Оцени тезисы в сообщении, на которое ответили." if target else "Что скажешь?")
        style = await self._style(conn, job)
        user = (f"Чат «{title}», сейчас {now} ({tz.key}). Запрос от {who}: {question}\n"
                + (f"{style}\n" if style else "") + f"Сценарий: {job.scenario}\n\n" + "\n\n".join(blocks))
        system = prompts.SYSTEM.format(ticket=ticket, web_marker=prompts.WEB_MARKER)
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]

    # ---- scheduled posts: the group digest, the wisdom of the day, the evening news ----

    async def run_daily_posts(self, now: datetime | None = None) -> None:
        """Called by the scheduler every tick: queue today's posts that are due and not made yet."""
        local = (now or datetime.now(timezone.utc)).astimezone(self.cfg.tz)
        async with self.pool.acquire() as conn:
            chats = await conn.fetch(
                "SELECT chat_id, settings FROM chats WHERE enabled AND migrated_to_chat_id IS NULL "
                "AND chat_id = ANY($1::bigint[])", list(self.cfg.allowed_chat_ids))
            for chat in chats:
                chat_id, settings = chat["chat_id"], chat["settings"] or {}
                for kind, when in (("digest", self.cfg.digest_time), ("wisdom", self.cfg.wisdom_time),
                                   ("news", self.cfg.news_time)):
                    if not self._post_on(chat_id, kind, settings) or local.time() < when:
                        continue
                    if kind == "digest" and not await daily.digest_due(conn, chat_id, local.date(),
                                                                       self._digest_days(settings)):
                        continue
                    # Still queued or running here: daily.claim would take it for one that died mid-way.
                    if (chat_id, kind) in self._posting:
                        continue
                    post_id = await daily.claim(conn, chat_id, kind, local.date())
                    if post_id is not None:
                        self._posting.add((chat_id, kind))
                        self._enqueue(_DIGESTS if kind == "digest" else _posts(chat_id),
                                      functools.partial(self._daily_post, chat_id, kind, local.date(), post_id))

    async def _daily_post(self, chat_id: int, kind: str, day: date, post_id: int) -> None:
        make = {"digest": self._digest, "wisdom": self._wisdom, "news": self._news}[kind]
        try:
            try:
                async with self.pool.acquire() as conn:
                    wanted = await self._still_wanted(conn, chat_id, kind)
                status, fields = await make(chat_id, day) if wanted else ("skipped", {})
            except Exception as e:  # noqa: BLE001 - retried by a later tick, see daily.claim
                log.warning("scheduled post failed",
                            extra={"chat_id": chat_id, "kind": kind, "error": type(e).__name__})
                status, fields = "failed", {}
            if kind == "digest" and status != "failed":     # notes are kept only for a retry of today's post
                self._digest_parts.pop(chat_id, None)
            async with self.pool.acquire() as conn:
                await daily.finish(conn, post_id, status, **fields)
        finally:
            self._posting.discard((chat_id, kind))
        log.info("scheduled post", extra={"chat_id": chat_id, "kind": kind, "status": status})

    async def _bot_is_member(self, chat_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id, self.me.user_id)
        except (TelegramForbiddenError, TelegramBadRequest):
            return False
        return member.status not in ("left", "kicked")

    async def _digest(self, chat_id: int, day: date) -> tuple[str, dict]:
        """The group digest posted on `day`: what the chat talked about in the N local days before it
        (/digest N, else DIGEST_EVERY_DAYS; with 1 the morning digest of yesterday), by the archive
        model. One call when the messages fit in it, else notes on parts first (DIGEST_CHUNK). The notes
        stay in _digest_parts while a retry of today's post may come (_daily_post drops them)."""
        if not await self._bot_is_member(chat_id):
            return "skipped", {}
        tz, limit = self.cfg.tz, self.cfg.daily_token_limit
        async with self.pool.acquire() as conn:
            spent, room = await self._digest_room(conn)
            if spent >= limit:
                return "skipped", {}
            chat = await conn.fetchrow("SELECT title, settings FROM chats WHERE chat_id = $1", chat_id) or {}
            days = self._digest_days(chat.get("settings"))
            since, until = daily.digest_bounds(day, days, tz)
            ids = await context.chat_lineage(conn, chat_id)
            kept = self._digest_parts.get(chat_id)      # left by a failed attempt
            if kept is not None and (kept.day, kept.days) != (day, days):     # another day's, or another period's
                del self._digest_parts[chat_id]
                kept = None
            if kept is not None:        # a retry of today's post reads the parts its first attempt read
                rows = [r for part in kept.parts for r in part]
            else:
                if days == 1:           # the morning digest of yesterday as it always was: its latest messages
                    rows = await context.period_messages(conn, ids, None, since, limit=DIGEST_CHUNK, until=until,
                                                         humans_only=True)
                else:
                    rows = await context.period_sample(conn, ids, since, until, DIGEST_MAX_MESSAGES, humans_only=True)
                fit = _digest_fit(rows, tz, room)
                if fit == 0:            # not even one call's worth left of the digests' half: the next morning
                    return "skipped", {}
                if fit < len(rows):
                    rows = await context.period_sample(conn, ids, since, until, fit, humans_only=True)
        if len(rows) < 5:
            return "skipped", {}
        request = f"Чат «{chat.get('title') or ''}», дайджест за {daily.digest_period(day, days)}."
        if kept is None and len(rows) <= DIGEST_CHUNK:
            data = context.render(rows, tz, title="сообщения за вчера" if days == 1 else "сообщения за эти дни")
        else:
            planned = kept is None      # this attempt measured the parts: their first notes fit
            if kept is None:
                n = len(rows)
                count = -(-n // DIGEST_CHUNK)
                kept = _DigestParts(day, days, [rows[i * n // count:(i + 1) * n // count] for i in range(count)])
                self._digest_parts[chat_id] = kept
            noted = await self._digest_notes(chat_id, request, kept, planned)
            if isinstance(noted, str):  # parts kept changing or did not fit: "failed"; switched off: "skipped"
                return noted, {}
            rows, notes = noted     # the messages as read last: a /forget_me meanwhile took some out
            if not rows:
                return "skipped", {}
            data = prompts.DIGEST_NOTES_INTRO + "\n\n" + notes
        async with self.pool.acquire() as conn:
            if not await self._still_wanted(conn, chat_id, "digest"):
                return "skipped", {}
        started = time.monotonic()
        prompt = [{"role": "system", "content": prompts.DIGEST_SYSTEM if days == 1 else prompts.DIGEST_PERIOD_SYSTEM},
                  {"role": "user", "content": f"{request}\n\n{data}"}]
        completion = await asyncio.wait_for(self.router.llm("digest").complete(prompt), self._timeout("digest"))
        text = daily.digest_header(day, days) + "\n\n" + completion.text
        # Always one message, cut if the model overran its length limit: a post is then either
        # delivered whole or retried whole, never left half-sent.
        refs, authors, oldest = await self._refs(chat_id, completion.text,
                                                 {r["message_id"]: r["user_id"] for r in rows})
        parts = markup.render(text, refs, chat_id, TELEGRAM_LIMIT - 1)
        first = parts[0] + ("…" if len(parts) > 1 else "")
        sent = await self._post(chat_id, first, html=True, plain=markup.to_plain(first))
        async with self.pool.acquire() as conn, conn.transaction():
            quoted = _quoted(refs, authors)
            if quoted:      # it may erase: the lock comes before the insert's row locks
                await forget.erase_lock(conn)
            rid = await self._log_scheduled(conn, chat_id, "digest", completion, [sent], started,
                                            status="ok" if sent else "error", quoted_user_ids=quoted,
                                            oldest_source_at=_oldest([oldest, *(r["stored_at"] for r in rows)]))
            await self._scrub_if_opted_out(conn, rid, quoted)
        if sent is None:
            return "failed", {}
        return "posted", {"message_id": sent.message_id}

    async def _digest_notes(self, chat_id: int, request: str, kept: _DigestParts,
                            planned: bool) -> tuple[list, str] | str:
        """Notes by the archive model on the parts of the period (kept.parts, at most DIGEST_CHUNK
        messages each): the messages they were written from and the notes, a block per part.

        Notes carry exact quotes and names, so the digest is written from them only while they match
        what the archive holds: each part is read again right before its call, and all of them once
        more after the last call; a part that changed meanwhile (someone used /forget_me, a message
        was edited, an author changed their name) gets new notes. The notes are kept by the text they
        were written from (kept.notes), so a retry of a failed post pays only for the parts that changed
        since. planned: this attempt measured the parts, so their first notes fit in the digests' half
        of the day; any other notes are made only while they fit in what is left of it (_digest_room).
        "failed" when parts still change after DIGEST_ROUNDS rounds or new notes do not fit, "skipped"
        when the group paused or switched the digest off meanwhile."""
        tz, count = self.cfg.tz, len(kept.parts)

        def span(part: list) -> str:
            return daily.span_name(part[0]["sent_at"].astimezone(tz).date(), part[-1]["sent_at"].astimezone(tz).date())

        keys: list[str | None] = [None] * count
        for round_ in range(DIGEST_ROUNDS):
            wrote = False
            for i in range(count):
                async with self.pool.acquire() as conn:
                    part = kept.parts[i] = await context.reread(conn, kept.parts[i])
                    if not part:
                        keys[i] = None
                        continue
                    shown = context.render(part, tz, title="сообщения")
                    keys[i] = hashlib.sha256(shown.encode()).hexdigest()
                    if keys[i] in kept.notes:
                        continue
                    if not await self._still_wanted(conn, chat_id, "digest"):
                        return "skipped"
                    if not (planned and round_ == 0):       # not measured: this call and the final one must fit
                        _, room = await self._digest_room(conn)
                        if len(shown) / DIGEST_CHARS_PER_TOKEN + 2 * DIGEST_CALL_TOKENS > room:
                            return "failed"
                started = time.monotonic()
                user = f"{request} Часть {i + 1} из {count}, {span(part)}.\n\n{shown}"
                prompt = [{"role": "system", "content": prompts.DIGEST_NOTES_SYSTEM}, {"role": "user", "content": user}]
                notes = await asyncio.wait_for(self.router.llm("digest").complete(prompt), self._timeout("digest"))
                async with self.pool.acquire() as conn:     # without the text: the digest made of it is what stays
                    await self._log_scheduled(conn, chat_id, "digest_notes", notes, [], started, keep_text=False,
                                              oldest_source_at=_oldest(r["stored_at"] for r in part))
                kept.notes[keys[i]] = notes.text
                wrote = True
            if not wrote:
                break
        else:
            return "failed"
        kept.notes = {key: kept.notes[key] for key in keys if key is not None}    # not those of older texts
        blocks = []
        for i, part in enumerate(kept.parts):
            if part:
                title = f"заметки, часть {i + 1} из {count}, {span(part)}"
                blocks.append(f"<{title}>\n{kept.notes[keys[i]] or '(заметок нет)'}\n</{title}>")
        return [r for part in kept.parts for r in part], "\n\n".join(blocks)

    async def _wisdom(self, chat_id: int, day: date) -> tuple[str, dict]:
        """A real quote for today's themes. The archive model picks themes from a fixed list;
        only those words reach the web-capable model, never the chat itself."""
        smart = self.router.llm("wisdom")
        if smart is None or not await self._bot_is_member(chat_id):
            return "skipped", {}
        tz = self.cfg.tz
        async with self.pool.acquire() as conn:
            if await self._tokens_today(conn) >= self.cfg.daily_token_limit:
                return "skipped", {}
            ids = await context.chat_lineage(conn, chat_id)
            rows = await context.period_messages(conn, ids, None, datetime.combine(day, dtime(0), tz), limit=400,
                                                 humans_only=True)
            recent = await daily.recent_wisdom(conn)
        themes: list[str] = []
        if len(rows) >= 5:
            started = time.monotonic()
            prompt = [{"role": "system", "content": prompts.THEMES_SYSTEM.format(themes=", ".join(daily.THEMES))},
                      {"role": "user", "content": context.render(rows, tz, title="сообщения за сегодня")}]
            try:
                found = await asyncio.wait_for(self.router.llm("topics").complete(prompt), self._timeout("topics"))
            except Exception as e:  # noqa: BLE001 - a quote without themes is still fine
                log.warning("theme extraction failed", extra={"chat_id": chat_id, "error": type(e).__name__})
            else:
                themes = daily.parse_themes(found.text)
                async with self.pool.acquire() as conn:     # the model's reply read the day's messages
                    await self._log_scheduled(conn, chat_id, "wisdom_themes", found, [], started,
                                              oldest_source_at=_oldest(r["stored_at"] for r in rows))
        started = time.monotonic()
        prompt = [{"role": "system", "content": prompts.WISDOM_SYSTEM},
                  {"role": "user", "content": daily.wisdom_request(themes, recent, day)}]
        completion = await asyncio.wait_for(smart.complete(prompt), self._timeout("wisdom"))
        wisdom = daily.parse_wisdom(completion.text, themes)
        if wisdom is None or daily.is_repeat(wisdom, recent):
            async with self.pool.acquire() as conn:
                await self._log_scheduled(conn, chat_id, "wisdom", completion, [], started, status="error")
            return "failed", {"themes": themes}
        card = daily.format_wisdom(wisdom)
        sent = await self._post(chat_id, card, html=True, plain=markup.to_plain(card))
        async with self.pool.acquire() as conn:
            await self._log_scheduled(conn, chat_id, "wisdom", completion, [sent], started,
                                      status="ok" if sent else "error")
        if sent is None:
            return "failed", {"themes": themes}
        return "posted", {"message_id": sent.message_id, "themes": themes, "quote": wisdom.quote,
                          "author": wisdom.author, "source_url": wisdom.source_url}

    async def _news(self, chat_id: int, day: date) -> tuple[str, dict]:
        """The evening news digest: written once a day from public sources (nestor.news), the
        same text for every group that has it on. No chat text is involved."""
        llm = self.router.llm("news")
        if self.news is None or llm is None or not await self._bot_is_member(chat_id):
            return "skipped", {}
        from ..news.service import NotEnoughNews

        try:
            text = await self.news.digest(day, llm, max_topics=self.cfg.news_max_topics,
                                          timeout_s=self._timeout("news"))
        except NotEnoughNews:
            # Sources not read yet (first start, or /news on after NEWS_TIME): a later tick tries again.
            return "waiting", {}
        if text is None:
            return "skipped", {}
        sent = await self._send(chat_id, text, html=True)
        if sent is None:
            return "failed", {}
        return "posted", {"message_id": sent.message_id}

    async def news_wanted(self) -> bool:
        """Whether any group gets the news digest: until then nothing is fetched."""
        async with self.pool.acquire() as conn:
            chats = await conn.fetch(
                "SELECT chat_id, settings FROM chats WHERE enabled AND migrated_to_chat_id IS NULL "
                "AND chat_id = ANY($1::bigint[])", list(self.cfg.allowed_chat_ids))
        return any(self._news_on(c["chat_id"], c["settings"] or {}) for c in chats)

    async def _news_status(self) -> str:
        """/news status: per source, when it was last read and what came of it over 7 days."""
        if self.news is None:
            return prompts.NEWS_STATE.format(state="не настроена", time=self.cfg.news_time.strftime("%H:%M"),
                                             topics=self.cfg.news_max_topics)
        from ..news import store as news_store

        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            states = await news_store.states(conn)
            counts = await news_store.counts_since(conn, now - timedelta(days=7))
            stale = set(await self.news.stale(conn, now))
        lines = [prompts.NEWS_STATUS_HEADER]
        for s in self.news.sources:
            st, c = states.get(s.key), counts.get(s.key, {})
            if st is None or st["last_run_at"] is None:
                lines.append(f"{s.key}: {prompts.NEWS_STATUS_NEVER}")
                continue
            when = (st["last_ok_at"] or st["last_run_at"]).astimezone(self.cfg.tz).strftime("%d.%m %H:%M")
            new = sum(c.get(k, 0) for k in ("new", "kept", "rejected", "used"))
            line = f"{s.key}: {st['status']}, {when}, {new} / {c.get('used', 0)} / {c.get('dropped', 0)}"
            if s.key in stale:
                line += f" — {prompts.NEWS_STATUS_STALE}"
            lines.append(line)
        return "\n".join(lines[:1] + [html.escape(line) for line in lines[1:]])

    async def _log_scheduled(self, conn, chat_id: int, scenario: str, completion: Completion,
                             sent: list[Message | None], started: float, status: str = "ok",
                             quoted_user_ids: list[int] | None = None,
                             oldest_source_at: datetime | None = None, keep_text: bool = True) -> int:
        """Scheduled posts have no asker; their tokens still count toward the daily limit.
        keep_text=False logs a call without its text (the digest's notes)."""
        return await conn.fetchval(
            """INSERT INTO bot_requests (chat_id, scenario, answer, model, input_tokens, output_tokens,
                                         latency_ms, status, answer_msg_ids, profile, quoted_user_ids,
                                         oldest_source_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                       least(now(), coalesce($12::timestamptz, now()))) RETURNING id""",
            chat_id, scenario, completion.text if keep_text else None, completion.model, completion.input_tokens,
            completion.output_tokens, int((time.monotonic() - started) * 1000), status,
            [m.message_id for m in sent if m is not None], completion.profile, quoted_user_ids, oldest_source_at)

    # ---- Telegram side ----

    async def _reply(self, msg: dict, text: str, *, save: bool = True, html: bool = False,
                     preview_url: str | None = None, keyboard: InlineKeyboardMarkup | None = None,
                     plain: str | None = None) -> Message | None:
        return await self._send(msg["chat"]["id"], text, reply_to=msg["message_id"], save=save, html=html,
                                preview_url=preview_url, keyboard=keyboard, plain=plain)

    async def _say(self, msg: dict, text: str, *, save: bool = True) -> Message | None:
        """One of the bot's own texts (prompts): Telegram HTML, plain text if Telegram refuses it."""
        return await self._reply(msg, text, save=save, html=True, plain=markup.to_plain(text))

    async def _post(self, chat_id: int, text: str, *, html: bool = False, plain: str | None = None) -> Message | None:
        """A message of the bot's own, not a reply (scheduled posts)."""
        return await self._send(chat_id, text, html=html, plain=plain)

    async def _send(self, chat_id: int, text: str, *, reply_to: int | None = None, save: bool = True,
                    html: bool = False, preview_url: str | None = None,
                    keyboard: InlineKeyboardMarkup | None = None, plain: str | None = None) -> Message | None:
        """plain: the same text without formatting, sent if Telegram refuses the HTML."""
        preview = (LinkPreviewOptions(url=preview_url, prefer_large_media=True, show_above_text=True) if preview_url
                   else LinkPreviewOptions(is_disabled=True))

        async def send(body: str, formatted: bool) -> Message:
            return await self.bot.send_message(
                chat_id, body,
                reply_parameters=(ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                                  if reply_to is not None else None),
                link_preview_options=preview, parse_mode="HTML" if formatted else None, reply_markup=keyboard,
            )

        try:
            sent = await send(text, html)
        except TelegramBadRequest as e:
            if not (html and plain):
                log.warning("send failed", extra={"chat_id": chat_id, "error": type(e).__name__})
                return None
            log.warning("formatting refused, sending plain text", extra={"chat_id": chat_id})
            try:
                sent = await send(plain, False)
            except TelegramAPIError as e2:
                log.warning("send failed", extra={"chat_id": chat_id, "error": type(e2).__name__})
                return None
        except TelegramAPIError as e:
            log.warning("send failed", extra={"chat_id": chat_id, "error": type(e).__name__})
            return None
        if save:
            await self._archive(sent, chat_id)
        return sent

    async def _archive(self, sent: Message, chat_id: int) -> None:
        """Telegram never sends the bot its own messages; archive them ourselves. The text is
        already delivered, so a failure here is logged, never answered with an apology."""
        if not isinstance(sent, Message):
            return
        try:
            await self.save_outgoing(dump(sent))
        except Exception as e:  # noqa: BLE001
            log.warning("archiving own message failed", extra={
                "chat_id": chat_id, "message_id": sent.message_id, "error": type(e).__name__})

    async def _react(self, msg: dict, emoji: str | None) -> None:
        try:
            await self.bot.set_message_reaction(msg["chat"]["id"], msg["message_id"],
                                                reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else [])
        except TelegramAPIError:
            if emoji:  # reactions restricted in this chat: show "typing…" instead
                try:
                    await self.bot.send_chat_action(msg["chat"]["id"], "typing",
                                                    message_thread_id=_topic(msg))
                except TelegramAPIError:
                    pass


# An earlier turn of a conversation, as the follow-ups read it.
_TURN = "id, chat_id, trigger_msg_id, question, answer, scenario, parent_id, quoted_user_ids, oldest_source_at"


def _oldest(times) -> datetime | None:
    """The earliest of the times that are known."""
    return min((t for t in times if t is not None), default=None)


def _out(completion: Completion, refs: dict[int, markup.Ref] | None = None) -> Outgoing:
    """A model answer: its Markdown becomes Telegram formatting."""
    return Outgoing(completion.text or "Мне нечего ответить по этим данным.", completion, markdown=True, refs=refs)


_LINKS = re.compile(r"https?://\S+?(?=[.,;:!?)»\"']*(?:\s|$))")      # an address, without its trailing punctuation


def _quoted(refs: dict[int, markup.Ref] | None, more: Collection[int] = ()) -> list[int] | None:
    """People whose words an answer carries (quote-links and the rest): /forget_me removes the
    answer with their messages."""
    users = sorted({r.user_id for r in (refs or {}).values() if r.user_id is not None}
                   | {u for u in more if u is not None})
    return users or None


def _digest_fit(rows: list, tz: ZoneInfo, budget: int) -> int:
    """How many of these messages (the digest's period, or an even sample of it) a digest may read for
    `budget` tokens, measured roughly (DIGEST_CHARS_PER_TOKEN, DIGEST_CALL_TOKENS): one call's worth at least
    (DIGEST_CHUNK, or all when fewer), and 0 when not even that fits."""
    if not rows:
        return 0
    per_message = len(context.render(rows, tz, title="сообщения")) / len(rows) / DIGEST_CHARS_PER_TOKEN
    if min(len(rows), DIGEST_CHUNK) * per_message + DIGEST_CALL_TOKENS > budget:
        return 0
    if len(rows) <= DIGEST_CHUNK:
        return len(rows)
    # m messages take ceil(m / DIGEST_CHUNK) calls of notes and the final one: at most m / DIGEST_CHUNK + 2.
    fit = (budget - 2 * DIGEST_CALL_TOKENS) / (per_message + DIGEST_CALL_TOKENS / DIGEST_CHUNK)
    return max(DIGEST_CHUNK, min(len(rows), int(fit)))


def _digest_change(arg: str) -> dict | None:
    """What /digest's argument changes in the chat's settings; None if it is not one."""
    if m := _DIGEST_DAYS.fullmatch(arg):
        days = int(m.group(1))
        return {"digest": True, "digest_days": days} if 1 <= days <= daily.DIGEST_MAX_DAYS else None
    if arg in _OFF or arg in _ON:
        return {"digest": arg in _ON}
    return None


def _numbered(text: str, n: int) -> list[str | None]:
    """«1. тема» lines -> [тема, ...] in order; missing numbers stay None."""
    names: list[str | None] = [None] * n
    for m in re.finditer(r"^\s*(\d+)[.)]\s*(.+?)\s*$", text or "", re.M):
        i = int(m.group(1))
        if 1 <= i <= n:
            names[i - 1] = m.group(2).strip(" «»\"")[:100] or None
    return names


def _snippet(text: str | None, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", text or "").strip() or "без текста"
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _shared_sender(msg: dict) -> bool:
    """Whether the sender is not one person: an anonymous admin, someone posting as a channel, Telegram's
    forwards from a linked channel. Their messages share one id, so /forget_me would forget everybody's."""
    return "sender_chat" in msg or msg["from"]["id"] in forget.SHARED_IDS or bool(msg["from"].get("is_bot"))


def _replied_author(msg: dict) -> int | None:
    """Who wrote the message the request replies to."""
    reply = _reply_target(msg)
    return ((reply or {}).get("from") or {}).get("id")


def _reply_target(msg: dict, inv: Invocation | None = None) -> dict | None:
    """The message a request replies to, unless it is the forum topic itself or the bot's answer."""
    reply = msg.get("reply_to_message")
    if not reply:
        return None
    if msg.get("is_topic_message") and reply.get("message_id") == msg.get("message_thread_id"):
        return None
    if inv is not None and inv.replied_to_bot is not None:
        return None
    return reply


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)


def _topic(msg: dict) -> int | None:
    return msg.get("message_thread_id") if msg.get("is_topic_message") else None
