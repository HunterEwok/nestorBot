"""Answers invocations: limits (FR-12), "thinking" signal (FR-13), one job per chat at a
time (FR-14), conversation continuation (FR-15), scenarios SC-1..SC-6, audit log.

Besides answers, the bot speaks on its own only when someone asked for it: reminders,
the morning digest and the wisdom of the day (started by nestor.bot.scheduler), and the
/export file in a private chat.
"""
from __future__ import annotations

import asyncio
import functools
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError)
from aiogram.types import (BufferedInputFile, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReactionTypeEmoji,
                           ReplyParameters)

from ..collector.app import dump
from . import context, daily, export, markup, music, prompts, reactions, reminders, routing
from .llm import LLM, Completion
from .markup import TELEGRAM_LIMIT, split_text
from .triggers import BotIdentity, Invocation, detect

log = logging.getLogger("nestor.answerer")

ANONYMOUS_ADMIN_ID = 1087968824  # GroupAnonymousBot: an admin posting as the group
_OFF = ("off", "выкл", "0", "нет", "no")


@dataclass(frozen=True)
class AnswerConfig:
    rate_limit: int = 5                 # calls per user ...
    rate_window_min: int = 10           # ... per this many minutes
    daily_token_limit: int = 250_000    # all chats together; soft: a started answer finishes
    timeout_s: float = 120
    progress_after_s: float = 60
    retention_months: int = 12
    tz: ZoneInfo = ZoneInfo("Europe/Tallinn")
    smart_timeout_s: float = 240        # songs and the wisdom: a bigger model and several web searches
    music_daily_limit: int = 10         # songs per day, all chats together
    music_player_app: str = ""          # short name of the player Mini App in BotFather; empty: none
    max_reminders: int = 20             # pending reminders per person and chat
    allowed_chat_ids: frozenset[int] = frozenset()   # scheduled posts go only to these chats
    wisdom_chat_ids: frozenset[int] = frozenset()    # wisdom of the day on by default; /wisdom on|off overrides
    digest_time: dtime = dtime(9, 0)
    wisdom_time: dtime = dtime(21, 0)
    news_chat_ids: frozenset[int] = frozenset()      # news digest on by default; /news on|off overrides
    news_time: dtime = dtime(20, 0)
    news_max_topics: int = 10
    reaction_chat_ids: frozenset[int] = frozenset()  # reactions on by default; /reactions on|off overrides
    reactions: reactions.ReactionConfig = reactions.ReactionConfig()


@dataclass
class Job:
    msg: dict
    inv: Invocation
    scenario: str
    question: str
    since: datetime | None = None       # /summary period, «что я пропустил»
    think: bool = False                 # «нестор, подумай …»: the bigger model


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
        self._queues: dict[int, asyncio.Queue[Callable[[], Awaitable[None]]]] = {}
        self._workers: dict[int, asyncio.Task] = {}

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
        if paused and inv.command != "resume":
            return
        if inv.kind == "command":
            await self._command(msg, inv)
            return
        thanks = reactions.acknowledgement(inv.text)
        if thanks is not None:   # «спасибо», «ок»: a reaction says it, no model needed
            await self._put_reaction(msg, thanks)
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
            await self._reply(msg, prompts.PRIVATE_HELP, save=False)

    def submit(self, job: Job) -> None:
        self._enqueue(job.msg["chat"]["id"], functools.partial(self._answer, job))

    def _enqueue(self, chat_id: int, run: Callable[[], Awaitable[None]]) -> None:
        queue = self._queues.setdefault(chat_id, asyncio.Queue())
        queue.put_nowait(run)
        if chat_id not in self._workers or self._workers[chat_id].done():
            self._workers[chat_id] = asyncio.create_task(self._worker(chat_id))

    async def drain(self) -> None:
        """Wait until all queued jobs are done (tests, shutdown)."""
        for queue in list(self._queues.values()):
            await queue.join()
        await self.reactor.drain()

    async def _worker(self, chat_id: int) -> None:
        queue = self._queues[chat_id]
        while not queue.empty():
            run = await queue.get()
            try:
                await run()
            except Exception:  # noqa: BLE001 - one failed job must not stop the chat's queue
                log.exception("job failed", extra={"chat_id": chat_id})
            finally:
                queue.task_done()

    # ---- commands ----

    async def _command(self, msg: dict, inv: Invocation) -> None:
        chat_id, user_id = msg["chat"]["id"], msg["from"]["id"]
        cmd = inv.command
        if cmd in ("help", "start"):
            await self._reply(msg, prompts.HELP.format(username=self.me.username))
        elif cmd == "privacy":
            await self._reply(msg, prompts.PRIVACY.format(retention=self.cfg.retention_months))
        elif cmd == "forget_me":
            count = await self.forget_user(user_id)
            await self._reply(msg, prompts.FORGOTTEN.format(count=count), save=False)
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
        elif cmd in ("pause", "resume", "bot_word", "digest", "wisdom", "news", "reactions"):
            if not await self._is_admin(msg):
                await self._reply(msg, prompts.ADMINS_ONLY)
                return
            if cmd == "news" and inv.args.strip().lower() in ("status", "статус"):
                await self._reply(msg, await self._news_status())
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
                    await self._reply(msg, prompts.BOT_WORD.format(state="включено" if on else "выключено"))
                    return
                await conn.execute("UPDATE chats SET enabled = $2 WHERE chat_id = $1", chat_id, cmd == "resume")
            await self._reply(msg, prompts.RESUMED if cmd == "resume" else prompts.PAUSED)

    async def _toggle(self, msg: dict, key: str, args: str) -> None:
        """/digest, /wisdom, /news, /reactions on|off; without an argument shows the current state."""
        chat_id, arg = msg["chat"]["id"], args.strip().lower()
        async with self.pool.acquire() as conn:
            if arg:
                await conn.execute(
                    "UPDATE chats SET settings = settings || jsonb_build_object($2::text, $3::bool) WHERE chat_id = $1",
                    chat_id, key, arg not in _OFF)
            settings = await conn.fetchval("SELECT settings FROM chats WHERE chat_id = $1", chat_id) or {}
        if key == "digest":
            on = bool(settings.get("digest", False))
            text = prompts.DIGEST_STATE.format(state="включён" if on else "выключен",
                                               time=self.cfg.digest_time.strftime("%H:%M"))
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
        await self._reply(msg, text)

    def _wisdom_on(self, chat_id: int, settings: dict) -> bool:
        value = (settings or {}).get("wisdom")
        return bool(value) if value is not None else chat_id in self.cfg.wisdom_chat_ids

    def _news_on(self, chat_id: int, settings: dict) -> bool:
        value = (settings or {}).get("news")
        return bool(value) if value is not None else chat_id in self.cfg.news_chat_ids

    def _reactions_on(self, chat_id: int, settings: dict) -> bool:
        value = (settings or {}).get("reactions")
        return bool(value) if value is not None else chat_id in self.cfg.reaction_chat_ids

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

    async def _is_admin(self, msg: dict) -> bool:
        if msg["from"]["id"] == ANONYMOUS_ADMIN_ID and (msg.get("sender_chat") or {}).get("id") == msg["chat"]["id"]:
            return True
        try:
            member = await self.bot.get_chat_member(msg["chat"]["id"], msg["from"]["id"])
        except TelegramAPIError:
            return False
        return member.status in ("creator", "administrator")

    async def forget_user(self, user_id: int) -> int:
        """/forget_me: delete the user's messages and reminders everywhere and stop storing new ones."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO users (user_id, opted_out) VALUES ($1, TRUE)
                   ON CONFLICT (user_id) DO UPDATE SET opted_out = TRUE, updated_at = now()""", user_id)
            status = await conn.execute("DELETE FROM messages WHERE user_id = $1", user_id)
            # Quotes of their words inside other people's replies.
            await conn.execute(
                """UPDATE messages SET raw = raw - 'quote'
                   WHERE raw ? 'quote' AND raw->'reply_to_message'->'from'->>'id' = $1::text""", str(user_id))
            await conn.execute("UPDATE bot_requests SET question = NULL WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM reminders WHERE user_id = $1", user_id)
        return int(status.split()[-1])

    # ---- reminders ----

    async def _set_reminder(self, job: Job) -> Outgoing:
        msg, now = job.msg, datetime.now(timezone.utc)
        try:
            parsed = reminders.parse(job.question, now, self.cfg.tz)
        except reminders.ReminderError as e:
            return Outgoing(str(e))
        if parsed is None:
            return Outgoing(prompts.REMINDER_NO_TIME)
        chat_id, user_id = msg["chat"]["id"], msg["from"]["id"]
        async with self.pool.acquire() as conn:
            if await reminders.count_pending(conn, chat_id, user_id) >= self.cfg.max_reminders:
                return Outgoing(prompts.REMINDERS_TOO_MANY.format(limit=self.cfg.max_reminders))
            rid = await reminders.create(conn, chat_id=chat_id, thread_id=_topic(msg), user_id=user_id,
                                         request_msg_id=msg["message_id"], text=parsed.text, due=parsed.due)
        return Outgoing(prompts.REMINDER_SET.format(when=reminders.format_due(parsed.due, self.cfg.tz, now),
                                                    about=reminders.about(parsed.text), id=rid))

    async def _list_reminders(self, msg: dict) -> None:
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            rows = await reminders.pending_for(conn, msg["chat"]["id"], msg["from"]["id"])
        if not rows:
            await self._reply(msg, prompts.REMINDERS_EMPTY)
            return
        items = "\n".join(prompts.REMINDER_ITEM.format(
            id=r["id"], when=reminders.format_due(r["due_at"], self.cfg.tz, now), about=reminders.about(r["text"]))
            for r in rows)
        await self._reply(msg, prompts.REMINDERS_LIST.format(items=items))

    async def _unremind(self, msg: dict, args: str) -> None:
        m = re.search(r"\d+", args or "")
        if not m:
            await self._reply(msg, prompts.UNREMIND_USAGE)
            return
        rid = int(m.group())
        async with self.pool.acquire() as conn:
            done = await reminders.cancel(conn, rid, msg["chat"]["id"], msg["from"]["id"])
        if not done and await self._is_admin(msg):  # admins may cancel anyone's reminder in their group
            async with self.pool.acquire() as conn:
                done = await reminders.cancel(conn, rid, msg["chat"]["id"], None)
        await self._reply(msg, prompts.UNREMIND_DONE.format(id=rid) if done else prompts.UNREMIND_NOT_FOUND)

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
                sent = await self.bot.send_message(
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
            await self._archive(sent, r["chat_id"])
            delivered += 1
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
            await self._reply(msg, prompts.EXPORT_TOO_OFTEN, save=not private)
        elif not rows:
            await self._reply(msg, prompts.EXPORT_EMPTY, save=not private)
        else:
            now = datetime.now(timezone.utc)
            document = BufferedInputFile(export.render(rows, user, self.cfg.tz, now),
                                         filename=export.filename(now, self.cfg.tz))
            try:
                await self.bot.send_document(user["id"], document,
                                             caption=prompts.EXPORT_CAPTION.format(count=len(rows)))
            except (TelegramForbiddenError, TelegramBadRequest):
                status = "blocked"  # the person has not opened a private chat with the bot yet
                await self._reply(msg, prompts.EXPORT_START.format(username=self.me.username), save=not private)
            except TelegramAPIError as e:
                status = "error"
                log.warning("export failed", extra={"chat_id": msg["chat"]["id"], "error": type(e).__name__})
                await self._reply(msg, prompts.ERROR, save=not private)
            else:
                if not private:
                    await self._reply(msg, prompts.EXPORT_SENT)
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

    async def _log(self, conn, job: Job, status: str, **fields) -> int:
        msg = job.msg
        return await conn.fetchval(
            """INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, thread_id, scenario, question,
                                         answer, model, input_tokens, output_tokens, latency_ms, status,
                                         answer_msg_ids, parent_id, profile)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) RETURNING id""",
            msg["chat"]["id"], msg["message_id"], msg["from"]["id"], msg.get("message_thread_id"),
            job.scenario, job.question, fields.get("answer"), fields.get("model"),
            fields.get("input_tokens"), fields.get("output_tokens"), fields.get("latency_ms"), status,
            fields.get("answer_msg_ids"), fields.get("parent_id"), fields.get("profile"),
        )

    async def _answer(self, job: Job) -> None:
        msg = job.msg
        async with self.pool.acquire() as conn:
            reason = await self._limit_reason(conn, msg["from"]["id"])
            if reason:
                await self._log(conn, job, "rate_limited")
        if reason:
            await self._reply(msg, reason)
            return

        await self._react(msg, "👀")
        ticket = secrets.token_urlsafe(18)
        started = time.monotonic()
        status, fields = "ok", {}
        try:
            async with self.pool.acquire() as conn:
                parent = await self._parent(conn, job)
            fields["parent_id"] = parent["id"] if parent else None
            out = await self._produce(job, parent, ticket)
            status = out.status
            sent = await self._deliver(msg, out)
            fields.update(answer=out.log_text or out.text, answer_msg_ids=[m.message_id for m in sent if m is not None])
            if out.completion is not None:
                c = out.completion
                fields.update(model=c.model, input_tokens=c.input_tokens, output_tokens=c.output_tokens,
                              profile=c.profile)
        except asyncio.TimeoutError:
            status = "timeout"
            await self._reply(msg, prompts.TIMEOUT)
        except Exception as e:  # noqa: BLE001 - LLMError, network, Hermes down: one short apology
            status = "error"
            log.warning("llm call failed", extra={"chat_id": msg["chat"]["id"], "error": type(e).__name__})
            await self._reply(msg, prompts.ERROR)
        finally:
            fields["latency_ms"] = int((time.monotonic() - started) * 1000)
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM request_tickets WHERE ticket = $1 OR expires_at < now()", ticket)
                await self._log(conn, job, status, **fields)
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
                return Outgoing(nothing)

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
        return _out(completion, await self._refs(job.msg["chat"]["id"], completion.text))

    async def _deliver(self, msg: dict, out: Outgoing) -> list[Message | None]:
        if out.html or out.keyboard or out.preview_url:
            return [await self._reply(msg, out.text, html=out.html, preview_url=out.preview_url, keyboard=out.keyboard)]
        if out.markdown:
            return [await self._reply(msg, part, html=True, plain=markup.to_plain(part))
                    for part in markup.render(out.text, out.refs, msg["chat"]["id"])]
        return [await self._reply(msg, part) for part in split_text(out.text)]

    async def _refs(self, chat_id: int, text: str) -> dict[int, markup.Ref]:
        """The archive messages an answer refers to (#id, [«…»](#id), t.me/c links), with
        their links and a piece of their text. Deleted messages and authors who used
        /forget_me are not found, so their references stay without a link or quote."""
        ids = markup.ref_ids(text, chat_id)
        if not ids:
            return {}
        refs: dict[int, markup.Ref] = {}
        async with self.pool.acquire() as conn:
            for cid in await context.chat_lineage(conn, chat_id):
                for r in await context.messages_by_id(conn, cid, [i for i in ids if i not in refs]):
                    refs[r["message_id"]] = markup.Ref(
                        context.message_link(r["chat_id"], r["message_id"], r["thread_id"], r["is_topic_message"]),
                        markup.quote(r["text"]))
        return refs

    async def _parent(self, conn, job: Job):
        """The earlier request whose answer this message replies to, if any."""
        if job.inv.replied_to_bot is None:
            return None
        return await conn.fetchrow(
            "SELECT id, chat_id, trigger_msg_id, question, answer, scenario, parent_id FROM bot_requests "
            "WHERE chat_id = $1 AND $2 = ANY(answer_msg_ids)", job.msg["chat"]["id"], job.inv.replied_to_bot)

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
            return Outgoing(prompts.NO_WEB)
        if _reply_target(job.msg, job.inv) is None:
            return Outgoing(prompts.FACTCHECK_HOW)
        async with self.pool.acquire() as conn:
            claim, opted_out = await self._replied_text(conn, job.msg)
            style = await self._style(conn, job)
        if claim is None:
            return Outgoing(prompts.REPLIED_OPTED_OUT if opted_out else prompts.FACTCHECK_NO_TEXT)
        now = datetime.now(self.cfg.tz).strftime("%Y-%m-%d %H:%M")
        user = (f"Сейчас {now} ({self.cfg.tz.key}). Утверждение из чата: «{claim}»\n"
                f"Вопрос участника: {job.question or 'проверь'}\n{style}")
        prompt = [{"role": "system", "content": prompts.FACTCHECK_SYSTEM}, {"role": "user", "content": user}]
        return _out(await self._complete_with_progress(job.msg, prompt, llm,
                                                       timeout=self._timeout("think_web" if job.think else "factcheck")))

    async def _music(self, job: Job, parent) -> Outgoing:
        """«нестор, включи …»: the smart profile finds the song, music.build_card checks the links."""
        llm = self.router.llm("music")
        if llm is None:
            return Outgoing(prompts.NO_WEB)
        extra = None
        async with self.pool.acquire() as conn:
            used = await conn.fetchval(
                "SELECT count(*) FROM bot_requests WHERE scenario = 'music' AND status = 'ok' AND created_at >= $1",
                self._day_start())
            if used >= self.cfg.music_daily_limit:
                return Outgoing(prompts.MUSIC_LIMIT.format(limit=self.cfg.music_daily_limit), status="rate_limited")
            if parent is None and music.needs_reply_text(job.question):
                extra, opted_out = await self._replied_text(conn, job.msg)
                if extra is None:
                    return Outgoing(prompts.REPLIED_OPTED_OUT if opted_out else prompts.MUSIC_NO_TEXT)
            history = await self._history(conn, job, ("music",))
        user = f"Запрос: {job.question}"
        if extra:
            user += f"\nПесня из сообщения, на которое ответили: «{extra[:500]}»"
        prompt = [{"role": "system", "content": prompts.MUSIC_SYSTEM}, *history, {"role": "user", "content": user}]
        completion = await self._complete_with_progress(job.msg, prompt, llm, timeout=self._timeout("music"))
        pick = music.parse(completion.text)
        if pick is None:
            return Outgoing(prompts.MUSIC_NOT_FOUND, completion, log_text=completion.text)
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
                return Outgoing(prompts.STATS_EMPTY)
            found = await context.top_branches(conn, ids, None, since)
            branches = []
            for b in found:
                members = list(b["members"])
                rows = await context.messages_by_id(conn, b["chat_id"], [b["root"]] + members[:6] + members[-3:])
                if rows:
                    branches.append((b, rows))
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
            name = html.escape((names[i - 1] if i <= len(names) else None) or _snippet(first["text"]))
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
                  {"role": "user", "content": f"Сейчас {now} ({self.cfg.tz.key}). Вопрос: {job.question}\n{style}"}]
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
                           {"role": "assistant", "content": parent["answer"] or ""}]
            parent = await conn.fetchrow(
                "SELECT id, chat_id, trigger_msg_id, question, answer, scenario, parent_id FROM bot_requests "
                "WHERE id = $1", parent["parent_id"]) if parent["parent_id"] else None
        return history

    async def _complete_with_progress(self, msg: dict, prompt: list[dict], llm: LLM | None = None,
                                      timeout: float | None = None):
        total = timeout or self.cfg.timeout_s
        task = asyncio.ensure_future((llm or self.llm).complete(prompt))
        try:
            return await asyncio.wait_for(asyncio.shield(task), min(self.cfg.progress_after_s, total))
        except asyncio.TimeoutError:
            note = await self._reply(msg, prompts.STILL_THINKING, save=False)
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
        Only a length, never chat text, so the web profiles may get it too."""
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

        reply = msg.get("reply_to_message") or {}
        target = reply.get("message_id")
        if target and (msg.get("is_topic_message") and target == msg.get("message_thread_id")):
            target = None
        if target and job.inv.replied_to_bot is None:
            chain = await context.reply_chain(conn, chat_id, target)
            below = await context.replies_to(conn, chat_id, target)
            blocks.append(f"Запрос — ответ на сообщение #{target}; оно и его ветка:")
            blocks.append(context.render(chain + [r for r in below if r["message_id"] != msg["message_id"]],
                                         tz, title="ветка"))

        for user in await context.mentioned_users(conn, msg, self.me.username):
            rows = await context.user_messages(conn, ids, thread, user["user_id"])
            blocks.append(context.render(rows, tz, title=f"сообщения {context.author(user)} за сутки"))

        if job.since is not None:
            rows = await context.period_messages(conn, ids, thread, job.since, limit=500)
            blocks.append(context.render([r for r in rows if r["message_id"] != msg["message_id"]], tz,
                                         title="сообщения за период"))
            if job.scenario == "catchup":
                rows = await context.addressed_to(conn, ids, thread, msg["from"]["id"], msg["from"].get("username"),
                                                  job.since)
                blocks.append(context.render(rows, tz, title=f"ответы и упоминания для {who}"))
        else:
            rows = await context.recent(conn, ids, thread)
            blocks.append(context.render([r for r in rows if r["message_id"] != msg["message_id"]], tz,
                                         title="последние сообщения чата"))

        history: list[dict] = []
        depth = 0
        while parent and depth < 3:
            history[:0] = [{"role": "user", "content": parent["question"] or "(вопрос удалён)"},
                           {"role": "assistant", "content": parent["answer"] or ""}]
            parent = await conn.fetchrow("SELECT id, question, answer, scenario, parent_id FROM bot_requests "
                                         "WHERE id = $1", parent["parent_id"]) if parent["parent_id"] else None
            depth += 1

        title = msg["chat"].get("title") or ""
        now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        question = job.question or ("Оцени тезисы в сообщении, на которое ответили." if target else "Что скажешь?")
        user = (f"Чат «{title}», сейчас {now} ({tz.key}). Запрос от {who}: {question}\n"
                f"{await self._style(conn, job)}\nСценарий: {job.scenario}\n\n" + "\n\n".join(blocks))
        system = prompts.SYSTEM.format(ticket=ticket, web_marker=prompts.WEB_MARKER)
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]

    # ---- scheduled posts: the morning digest, the wisdom of the day, the evening news ----

    async def run_daily_posts(self, now: datetime | None = None) -> None:
        """Called by the scheduler every tick: queue today's posts that are due and not made yet."""
        local = (now or datetime.now(timezone.utc)).astimezone(self.cfg.tz)
        async with self.pool.acquire() as conn:
            chats = await conn.fetch(
                "SELECT chat_id, settings FROM chats WHERE enabled AND migrated_to_chat_id IS NULL "
                "AND chat_id = ANY($1::bigint[])", list(self.cfg.allowed_chat_ids))
            for chat in chats:
                chat_id, settings = chat["chat_id"], chat["settings"] or {}
                due = []
                if settings.get("digest") and local.time() >= self.cfg.digest_time:
                    due.append("digest")
                if self._wisdom_on(chat_id, settings) and local.time() >= self.cfg.wisdom_time:
                    due.append("wisdom")
                if self.news is not None and self._news_on(chat_id, settings) and local.time() >= self.cfg.news_time:
                    due.append("news")
                for kind in due:
                    post_id = await daily.claim(conn, chat_id, kind, local.date())
                    if post_id is not None:
                        self._enqueue(chat_id, functools.partial(self._daily_post, chat_id, kind, local.date(), post_id))

    async def _daily_post(self, chat_id: int, kind: str, day: date, post_id: int) -> None:
        make = {"digest": self._digest, "wisdom": self._wisdom, "news": self._news}[kind]
        try:
            status, fields = await make(chat_id, day)
        except Exception as e:  # noqa: BLE001 - retried by a later tick, see daily.claim
            log.warning("scheduled post failed", extra={"chat_id": chat_id, "kind": kind, "error": type(e).__name__})
            status, fields = "failed", {}
        async with self.pool.acquire() as conn:
            await daily.finish(conn, post_id, status, **fields)
        log.info("scheduled post", extra={"chat_id": chat_id, "kind": kind, "status": status})

    async def _bot_is_member(self, chat_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id, self.me.user_id)
        except (TelegramForbiddenError, TelegramBadRequest):
            return False
        return member.status not in ("left", "kicked")

    async def _digest(self, chat_id: int, day: date) -> tuple[str, dict]:
        """Morning summary of the previous local day, by the archive model."""
        if not await self._bot_is_member(chat_id):
            return "skipped", {}
        tz = self.cfg.tz
        yesterday = day - timedelta(days=1)
        async with self.pool.acquire() as conn:
            if await self._tokens_today(conn) >= self.cfg.daily_token_limit:
                return "skipped", {}
            ids = await context.chat_lineage(conn, chat_id)
            rows = await context.period_messages(conn, ids, None, datetime.combine(yesterday, dtime(0), tz), limit=500,
                                                 until=datetime.combine(day, dtime(0), tz), humans_only=True)
            title = await conn.fetchval("SELECT title FROM chats WHERE chat_id = $1", chat_id) or ""
        if len(rows) < 5:
            return "skipped", {}
        started = time.monotonic()
        prompt = [{"role": "system", "content": prompts.DIGEST_SYSTEM},
                  {"role": "user", "content": f"Чат «{title}», дайджест за {daily.day_name(yesterday)}.\n\n"
                                              + context.render(rows, tz, title="сообщения за вчера")}]
        completion = await asyncio.wait_for(self.router.llm("digest").complete(prompt), self._timeout("digest"))
        text = prompts.DIGEST_HEADER.format(date=daily.day_name(yesterday)) + "\n\n" + completion.text
        # Always one message, cut if the model overran its length limit: a post is then either
        # delivered whole or retried whole, never left half-sent.
        parts = markup.render(text, await self._refs(chat_id, completion.text), chat_id, TELEGRAM_LIMIT - 1)
        first = parts[0] + ("…" if len(parts) > 1 else "")
        sent = await self._post(chat_id, first, html=True, plain=markup.to_plain(first))
        async with self.pool.acquire() as conn:
            await self._log_scheduled(conn, chat_id, "digest", completion, [sent], started,
                                      status="ok" if sent else "error")
        if sent is None:
            return "failed", {}
        return "posted", {"message_id": sent.message_id}

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
                async with self.pool.acquire() as conn:
                    await self._log_scheduled(conn, chat_id, "wisdom_themes", found, [], started)
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
        return "\n".join(lines)

    async def _log_scheduled(self, conn, chat_id: int, scenario: str, completion: Completion,
                             sent: list[Message | None], started: float, status: str = "ok") -> None:
        """Scheduled posts have no asker; their tokens still count toward the daily limit."""
        await conn.execute(
            """INSERT INTO bot_requests (chat_id, scenario, answer, model, input_tokens, output_tokens,
                                         latency_ms, status, answer_msg_ids, profile)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            chat_id, scenario, completion.text, completion.model, completion.input_tokens, completion.output_tokens,
            int((time.monotonic() - started) * 1000), status, [m.message_id for m in sent if m is not None],
            completion.profile)

    # ---- Telegram side ----

    async def _reply(self, msg: dict, text: str, *, save: bool = True, html: bool = False,
                     preview_url: str | None = None, keyboard: InlineKeyboardMarkup | None = None,
                     plain: str | None = None) -> Message | None:
        return await self._send(msg["chat"]["id"], text, reply_to=msg["message_id"], save=save, html=html,
                                preview_url=preview_url, keyboard=keyboard, plain=plain)

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


def _out(completion: Completion, refs: dict[int, markup.Ref] | None = None) -> Outgoing:
    """A model answer: its Markdown becomes Telegram formatting."""
    return Outgoing(completion.text or "Мне нечего ответить по этим данным.", completion, markdown=True, refs=refs)


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
