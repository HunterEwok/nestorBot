"""Answers invocations: limits (FR-12), "thinking" signal (FR-13), one job per chat at a
time (FR-14), conversation continuation (FR-15), scenarios SC-1..SC-6, audit log."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions, Message, ReactionTypeEmoji, ReplyParameters

from ..collector.app import dump
from . import context, prompts
from .llm import LLM, Completion
from .triggers import BotIdentity, Invocation, detect

log = logging.getLogger("nestor.answerer")

ANONYMOUS_ADMIN_ID = 1087968824  # GroupAnonymousBot: an admin posting as the group
TELEGRAM_LIMIT = 4096


@dataclass(frozen=True)
class AnswerConfig:
    rate_limit: int = 5                 # calls per user ...
    rate_window_min: int = 10           # ... per this many minutes
    daily_token_limit: int = 250_000    # all chats together; soft: a started answer finishes
    timeout_s: float = 120
    progress_after_s: float = 60
    retention_months: int = 12
    tz: ZoneInfo = ZoneInfo("Europe/Tallinn")


@dataclass
class Job:
    msg: dict
    inv: Invocation
    scenario: str
    question: str
    since: datetime | None = None       # /summary period


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """SC-6: split on paragraph, then line, then word boundaries."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit), rest.rfind(" ", 0, limit))
        if cut <= limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


SaveOutgoing = Callable[[dict], Awaitable[object]]


class Answerer:
    def __init__(self, bot: Bot, pool: asyncpg.Pool, llm: LLM, me: BotIdentity,
                 save_outgoing: SaveOutgoing, cfg: AnswerConfig = AnswerConfig(), web_llm: LLM | None = None):
        self.bot, self.pool, self.llm, self.me, self.cfg = bot, pool, llm, me, cfg
        # Profile with web search and no archive tools; it only ever sees the asker's own words.
        self.web_llm = web_llm
        self.save_outgoing = save_outgoing
        self._queues: dict[int, asyncio.Queue[Job]] = {}
        self._workers: dict[int, asyncio.Task] = {}

    # ---- entry point, called by the collector after a new message was stored ----

    async def on_message(self, msg: dict, *, paused: bool) -> None:
        async with self.pool.acquire() as conn:
            settings = await conn.fetchval("SELECT settings FROM chats WHERE chat_id = $1", msg["chat"]["id"]) or {}
        inv = detect(msg, self.me, generic_keyword_enabled=settings.get("keyword_bot", True))
        if inv is None or "from" not in msg:
            return
        if paused and inv.command != "resume":
            return
        if inv.kind == "command":
            await self._command(msg, inv)
            return
        question = inv.text
        self.submit(Job(msg, inv, context.classify(question), question))

    def submit(self, job: Job) -> None:
        chat_id = job.msg["chat"]["id"]
        queue = self._queues.setdefault(chat_id, asyncio.Queue())
        queue.put_nowait(job)
        if chat_id not in self._workers or self._workers[chat_id].done():
            self._workers[chat_id] = asyncio.create_task(self._worker(chat_id))

    async def drain(self) -> None:
        """Wait until all queued jobs are answered (tests, shutdown)."""
        for queue in list(self._queues.values()):
            await queue.join()

    async def _worker(self, chat_id: int) -> None:
        queue = self._queues[chat_id]
        while not queue.empty():
            job = await queue.get()
            try:
                await self._answer(job)
            except Exception:  # noqa: BLE001 - one failed job must not stop the chat's queue
                log.exception("answer failed", extra={"chat_id": chat_id})
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
            await self._reply(msg, await self._stats(chat_id))
        elif cmd == "summary":
            since = context.period_start(inv.args, datetime.now(timezone.utc), self.cfg.tz)
            question = f"Краткое содержание обсуждения {inv.args or 'за сегодня'}"
            self.submit(Job(msg, inv, "summary", question, since=since))
        elif cmd in ("pause", "resume", "bot_word"):
            if not await self._is_admin(msg):
                await self._reply(msg, prompts.ADMINS_ONLY)
                return
            async with self.pool.acquire() as conn:
                if cmd == "bot_word":
                    on = inv.args.strip().lower() not in ("off", "выкл", "0", "нет")
                    await conn.execute(
                        "UPDATE chats SET settings = settings || jsonb_build_object('keyword_bot', $2::bool) "
                        "WHERE chat_id = $1", chat_id, on)
                    await self._reply(msg, prompts.BOT_WORD.format(state="включено" if on else "выключено"))
                    return
                await conn.execute("UPDATE chats SET enabled = $2 WHERE chat_id = $1", chat_id, cmd == "resume")
            await self._reply(msg, prompts.RESUMED if cmd == "resume" else prompts.PAUSED)

    async def _is_admin(self, msg: dict) -> bool:
        if msg["from"]["id"] == ANONYMOUS_ADMIN_ID and (msg.get("sender_chat") or {}).get("id") == msg["chat"]["id"]:
            return True
        try:
            member = await self.bot.get_chat_member(msg["chat"]["id"], msg["from"]["id"])
        except TelegramAPIError:
            return False
        return member.status in ("creator", "administrator")

    async def forget_user(self, user_id: int) -> int:
        """/forget_me: delete the user's messages everywhere and stop storing new ones."""
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
        return int(status.split()[-1])

    async def _stats(self, chat_id: int) -> str:
        async with self.pool.acquire() as conn:
            ids = await context.chat_lineage(conn, chat_id)
            rows = await conn.fetch(
                """SELECT u.first_name, u.last_name, u.username, count(*) AS n
                   FROM messages m JOIN users u ON u.user_id = m.user_id
                   WHERE m.chat_id = ANY($1::bigint[]) AND m.kind <> 'service' AND NOT u.is_bot
                     AND m.sent_at > now() - interval '7 days'
                   GROUP BY u.user_id ORDER BY n DESC LIMIT 10""", ids)
        if not rows:
            return "За неделю сообщений нет."
        return "Активность за 7 дней:\n" + "\n".join(f"{i}. {context.author(r)} — {r['n']}" for i, r in enumerate(rows, 1))

    # ---- answering ----

    async def _limit_reason(self, conn, user_id: int) -> str | None:
        recent = await conn.fetchval(
            """SELECT count(*) FROM bot_requests
               WHERE user_id = $1 AND status <> 'rate_limited' AND created_at > now() - make_interval(mins => $2)""",
            user_id, self.cfg.rate_window_min)
        if recent >= self.cfg.rate_limit:
            return prompts.RATE_LIMITED_USER.format(limit=self.cfg.rate_limit, window=self.cfg.rate_window_min)
        day_start = datetime.now(self.cfg.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        used = await conn.fetchval(
            "SELECT coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) "
            "FROM bot_requests WHERE created_at >= $1", day_start)
        if used >= self.cfg.daily_token_limit:
            return prompts.RATE_LIMITED_DAY
        return None

    async def _log(self, conn, job: Job, status: str, **fields) -> int:
        msg = job.msg
        return await conn.fetchval(
            """INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, thread_id, scenario, question,
                                         answer, model, input_tokens, output_tokens, latency_ms, status,
                                         answer_msg_ids, parent_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING id""",
            msg["chat"]["id"], msg["message_id"], msg["from"]["id"], msg.get("message_thread_id"),
            job.scenario, job.question, fields.get("answer"), fields.get("model"),
            fields.get("input_tokens"), fields.get("output_tokens"), fields.get("latency_ms"), status,
            fields.get("answer_msg_ids"), fields.get("parent_id"),
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
            if self.web_llm is not None and (job.scenario == "web" or (parent and parent["scenario"] == "web")):
                # Plain web question or a follow-up to a web answer: the archive model is not needed.
                completion = await self._answer_from_web(job, None)
            else:
                async with self.pool.acquire() as conn:
                    prompt = await self._build_prompt(conn, job, ticket, parent)
                    await conn.execute(
                        "INSERT INTO request_tickets (ticket, chat_id, thread_id, expires_at) VALUES ($1, $2, $3, $4)",
                        ticket, msg["chat"]["id"], _topic(msg),
                        datetime.now(timezone.utc) + timedelta(seconds=self.cfg.timeout_s + 30))
                completion = await self._complete_with_progress(msg, prompt)
                if prompts.WEB_MARKER in completion.text:
                    completion = await self._answer_from_web(job, completion)
            sent = []
            for part in split_text(completion.text or "Мне нечего ответить по этим данным."):
                sent.append(await self._reply(msg, part))
            fields.update(answer=completion.text, model=completion.model,
                          input_tokens=completion.input_tokens, output_tokens=completion.output_tokens,
                          answer_msg_ids=[m.message_id for m in sent if m is not None])
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

    async def _parent(self, conn, job: Job):
        """The earlier request whose answer this message replies to, if any."""
        if job.inv.replied_to_bot is None:
            return None
        return await conn.fetchrow(
            "SELECT id, question, answer, scenario, parent_id FROM bot_requests "
            "WHERE chat_id = $1 AND $2 = ANY(answer_msg_ids)", job.msg["chat"]["id"], job.inv.replied_to_bot)

    async def _answer_from_web(self, job: Job, routed: Completion | None) -> Completion:
        """The archive model asked for the internet. The web profile gets the asker's question and
        earlier web turns of this conversation only: no archive text can reach a search engine."""
        job.scenario = "web"
        if self.web_llm is None or not job.question.strip():
            text = prompts.NO_WEB if self.web_llm is None else prompts.EMPTY_WEB_QUESTION
            return Completion(text, *((routed.model, routed.input_tokens, routed.output_tokens) if routed
                                      else (None, None, None)))
        async with self.pool.acquire() as conn:
            history = await self._web_history(conn, job)
        now = datetime.now(self.cfg.tz).strftime("%Y-%m-%d %H:%M")
        prompt = [{"role": "system", "content": prompts.WEB_SYSTEM}, *history,
                  {"role": "user", "content": f"Сейчас {now} ({self.cfg.tz.key}). Вопрос: {job.question}"}]
        web = await self._complete_with_progress(job.msg, prompt, self.web_llm)
        if routed is None:
            return web
        return Completion(web.text, web.model, _add(routed.input_tokens, web.input_tokens),
                          _add(routed.output_tokens, web.output_tokens))

    async def _web_history(self, conn, job: Job) -> list[dict]:
        """Earlier turns of a reply chain, as long as they were web answers too."""
        history: list[dict] = []
        parent = await self._parent(conn, job)
        while parent and parent["scenario"] == "web" and parent["question"] and len(history) < 6:
            history[:0] = [{"role": "user", "content": parent["question"]},
                           {"role": "assistant", "content": parent["answer"] or ""}]
            parent = await conn.fetchrow("SELECT question, answer, scenario, parent_id FROM bot_requests "
                                         "WHERE id = $1", parent["parent_id"]) if parent["parent_id"] else None
        return history

    async def _complete_with_progress(self, msg: dict, prompt: list[dict], llm: LLM | None = None):
        task = asyncio.ensure_future((llm or self.llm).complete(prompt))
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.cfg.progress_after_s)
        except asyncio.TimeoutError:
            note = await self._reply(msg, prompts.STILL_THINKING, save=False)
            try:
                return await asyncio.wait_for(task, self.cfg.timeout_s - self.cfg.progress_after_s)
            finally:
                if note is not None:
                    try:
                        await self.bot.delete_message(msg["chat"]["id"], note.message_id)
                    except TelegramAPIError:
                        pass

    async def _build_prompt(self, conn, job: Job, ticket: str, parent) -> list[dict]:
        msg, tz = job.msg, self.cfg.tz
        chat_id = msg["chat"]["id"]
        ids = await context.chat_lineage(conn, chat_id)
        thread = _topic(msg)
        blocks: list[str] = []

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
            rows = await context.period_messages(conn, ids, thread, job.since)
            blocks.append(context.render(rows, tz, title="сообщения за период"))
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
        who = context.author({"first_name": msg["from"].get("first_name"), "last_name": msg["from"].get("last_name"),
                              "username": msg["from"].get("username")})
        now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        question = job.question or ("Оцени тезисы в сообщении, на которое ответили." if target else "Что скажешь?")
        user = (f"Чат «{title}», сейчас {now} ({tz.key}). Запрос от {who}: {question}\n"
                f"Сценарий: {job.scenario}\n\n" + "\n\n".join(blocks))
        system = prompts.SYSTEM.format(ticket=ticket, web_marker=prompts.WEB_MARKER)
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]

    # ---- Telegram side ----

    async def _reply(self, msg: dict, text: str, *, save: bool = True) -> Message | None:
        try:
            sent = await self.bot.send_message(
                msg["chat"]["id"], text,
                reply_parameters=ReplyParameters(message_id=msg["message_id"], allow_sending_without_reply=True),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as e:
            log.warning("send failed", extra={"chat_id": msg["chat"]["id"], "error": type(e).__name__})
            return None
        if save and isinstance(sent, Message):
            # Telegram never sends the bot its own messages; archive them ourselves. The text is
            # already delivered, so a failure here is logged, never answered with an apology.
            try:
                await self.save_outgoing(dump(sent))
            except Exception as e:  # noqa: BLE001
                log.warning("archiving own message failed", extra={
                    "chat_id": msg["chat"]["id"], "message_id": sent.message_id, "error": type(e).__name__})
        return sent

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


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)


def _topic(msg: dict) -> int | None:
    return msg.get("message_thread_id") if msg.get("is_topic_message") else None
